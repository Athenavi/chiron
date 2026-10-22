"""启动前置校验与依赖访问器（从 `app/main.py` 纯移动而来）。

为什么单独立模块：`main.py` 已经 2000+ 行，而"启动时该拒绝启动什么"是**部署契约**，
值得有自己的文件与读者。`main.py` 通过 `from app.deps import ...` **再导出**，
既有调用路径（含 `app.main.verify_sandbox_root` 的文档引用）一律不变。

⚠ 本模块**只放不依赖 lifespan 局部状态**的东西：`main.py` 里的 `_redis` / `_gateway` /
`_plugin_pool` 等模块级状态与依赖取用器暂不搬迁 —— 它们被 lifespan 用 `global` 赋值，
跨模块移动会让"改的是哪个模块的名字"变得难以判断（见 docs/split-assessment.md 批次 4）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def verify_sandbox_root() -> None:
    """校验 agent 沙箱根（SANDBOX_ROOT）是否满足部署要求 —— 多副本安全的关键前置。

    背景：所有 agent 文件/命令/git 工具都被强制在
    ``SANDBOX_ROOT/{tenant}/{user}/workspace`` 内执行。该变量的缺省值是
    **进程本地路径**（cwd 上两级），单机开发可用；但多副本部署下，同一用户的任务
    落到不同副本时会各自维护一份 workspace —— 文件/命令工具表现为"间歇性失忆"。

    规则：
    - 生产环境（见 ``app/envmode.py``：``CHIRON_ENV`` / ``APP_ENV`` / ``PYTHON_ENV``
      任一为 ``production|prod``）必须显式设置 ``SANDBOX_ROOT``；
      未设置则打印**具体原因与修复指引**后拒绝启动（``CHIRON_ALLOW_LOCAL_SANDBOX=true`` 仅限单机开发放行）。
    - 非生产环境允许缺省，但打印 WARN 说明多副本要求。
    - 已显式配置、但解析结果落在当前工作目录内时给出 WARN（疑似仍是容器本地盘）。
    """
    from app.tools.sandbox import SANDBOX_ROOT_ENV, sandbox_root

    from app.envmode import env_name as _current_env_name, is_production

    # 生产判定走 app.envmode 的**唯一入口**：此前这里只看 CHIRON_ENV，而 config.py 的
    # fail-fast 看 APP_ENV/PYTHON_ENV ⇒ 只设其中一个时另一半门禁静默失效。
    env_name = _current_env_name()
    is_prod = is_production()
    allow_local = (os.getenv("CHIRON_ALLOW_LOCAL_SANDBOX") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    raw = (os.getenv(SANDBOX_ROOT_ENV) or "").strip()
    resolved = sandbox_root()

    if raw:
        logger.info("%s=%s (resolved: %s)", SANDBOX_ROOT_ENV, raw, resolved)
        if not allow_local and _is_inside_cwd(resolved):
            logger.warning(
                "%s resolves inside the process working directory (%s). Multi-replica "
                "deployments need a shared volume (NFS/PVC) — otherwise every replica "
                "keeps its own copy of user workspaces.",
                resolved,
                Path.cwd(),
            )
    else:
        logger.warning(
            "%s is not set — agent file/shell/git tools will use the process-local default "
            "(%s). Multi-replica deployments MUST set %s to a shared volume, otherwise "
            "a user's files written on one replica are invisible on another.",
            SANDBOX_ROOT_ENV,
            resolved,
            SANDBOX_ROOT_ENV,
        )

    if is_prod and not raw and not allow_local:
        logger.error(
            "Refusing to start: production detected (env_name=%s) but %s is not set "
            "(agent sandbox would fall back to the process-local path %s). "
            "Reason: %s/{tenant}/{user}/workspace is where all agent file, shell and git "
            "tools operate; with a per-replica path, a task handled by a different replica "
            "cannot see files written by another (intermittent 'amnesia' in file workflows). "
            "Fix: point %s at a shared volume, e.g. %s=/shared/chiron-sandbox; "
            "for single-node development only, bypass with CHIRON_ALLOW_LOCAL_SANDBOX=true.",
            env_name,
            SANDBOX_ROOT_ENV,
            resolved,
            SANDBOX_ROOT_ENV,
            SANDBOX_ROOT_ENV,
            SANDBOX_ROOT_ENV,
        )
        raise RuntimeError(
            f"{SANDBOX_ROOT_ENV} must point to a shared volume when env={env_name} "
            "(set CHIRON_ALLOW_LOCAL_SANDBOX=true for single-node development)"
        )


def verify_sandbox_backend() -> None:
    """校验 `SANDBOX_BACKEND` 取值合法 —— 拼错一个字母就等于关掉沙箱隔离。

    为什么必须 fail-fast（而不是打个 warning 继续）：`main.py` 的
    `_apply_sandbox_service_branch` 只认**精确等于** `service`（去空白转小写后）的取值，
    其余任何取值都走"恒等返回"分支 —— 也就是**静默地在引擎进程内执行**。而进程内沙箱
    自述不是安全边界（见 `app/backends/remote_exec.py` 的模块文档："它不是安全等级的
    跃升"），真正把逃逸影响面限制在只读根 / 无 capability / 无外网出口容器里的，是独立
    沙箱服务。

    于是「配置写错」与「隔离没生效」之间只差一个字母，而且**没有任何日志会说出来**
    （取值不精确等于 `service` 时，那段代码连 warning 都不打）。企业部署里这个变量常由
    模板或托管平台注入，拼错无人可察 —— 所以这里把取值收紧成硬校验。

    允许的取值：
    * `service`（默认，隔离生效）：必须同时给出 `SANDBOX_SERVICE_URL`；
    * `local`：**显式**退出隔离，允许（单机开发），不做 URL 校验；
    * 空：按默认值 `service` 处理（与 `app/config.py` 的字段默认一致）——
      "没写"应当等于安全默认，而不是悄悄退回进程内。
    """
    allowed = ("service", "local")
    raw = (os.getenv("SANDBOX_BACKEND") or "").strip()
    backend = raw.lower() or "service"

    if backend not in allowed:
        logger.error(
            "Refusing to start: SANDBOX_BACKEND=%r is not a valid value. "
            "Reason: only an exact 'service' activates the isolated sandbox service; any "
            "other value silently falls back to in-process execution, where the static "
            "guards are a speed bump rather than a boundary. "
            "Fix: SANDBOX_BACKEND=service (plus SANDBOX_SERVICE_URL), or explicitly "
            "SANDBOX_BACKEND=local to opt out.",
            raw,
        )
        raise RuntimeError(
            f"SANDBOX_BACKEND={raw!r} is invalid; allowed values are {allowed} "
            "(empty means 'service')"
        )

    if backend == "local":
        logger.warning(
            "SANDBOX_BACKEND=local — command/code execution runs inside the engine "
            "process (the in-process sandbox is not a security boundary). "
            "Use SANDBOX_BACKEND=service for isolated execution."
        )
        return

    url = (os.getenv("SANDBOX_SERVICE_URL") or "").strip()
    if not url:
        logger.error(
            "Refusing to start: SANDBOX_BACKEND=service but SANDBOX_SERVICE_URL is not set. "
            "Reason: without the URL the execution branch is skipped silently and every "
            "command runs in-process instead of in the isolated sandbox container. "
            "Fix: set SANDBOX_SERVICE_URL (e.g. http://sandbox-lb:8001), or explicitly opt "
            "out with SANDBOX_BACKEND=local."
        )
        raise RuntimeError(
            "SANDBOX_BACKEND=service requires SANDBOX_SERVICE_URL "
            "(or set SANDBOX_BACKEND=local to opt out of isolation)"
        )

    logger.info("Sandbox backend: service at %s (isolated execution)", url)


def verify_media_store() -> None:
    """校验媒体存储后端配置 —— 多副本下媒体文件必须对每个副本可见。

    - ``MEDIA_STORE_BACKEND=s3``：必须提供 ``S3_BUCKET`` 与 ``S3_ENDPOINT_URL``，
      缺失即拒绝启动（缺失会让写入回退到进程本地路径，多副本下生成物只在单副本可见）。
    - ``local``（默认）：打印 WARN 与落盘路径，提示多副本需切 s3。
    """
    backend = (os.getenv("MEDIA_STORE_BACKEND") or "local").strip().lower()

    if backend == "s3":
        missing = [
            key for key in ("S3_BUCKET", "S3_ENDPOINT_URL") if not (os.getenv(key) or "").strip()
        ]
        if missing:
            logger.error(
                "Refusing to start: MEDIA_STORE_BACKEND=s3 but %s missing. "
                "Reason: media writes would fall back to a process-local path, so files "
                "produced on one replica return 404 when served from another. "
                "Fix: set %s (plus S3_ACCESS_KEY/S3_SECRET_KEY when the bucket requires them).",
                ", ".join(missing),
                ", ".join(missing),
            )
            raise RuntimeError(
                "MEDIA_STORE_BACKEND=s3 requires S3_BUCKET and S3_ENDPOINT_URL"
            )
        logger.info(
            "Media store backend: s3 (bucket=%s, prefix=%s)",
            os.getenv("S3_BUCKET"),
            os.getenv("S3_PREFIX", "media/"),
        )
        return

    logger.warning(
        "Media store backend: local (path=%s). Multi-replica deployments should set "
        "MEDIA_STORE_BACKEND=s3 with S3_* so generated media is visible to every replica.",
        os.getenv("MEDIA_STORE_PATH", os.path.join(".", "data", "media")),
    )


def _is_inside_cwd(path: Path) -> bool:
    """判断路径是否位于当前工作目录内（用于识别"仍是容器本地盘"的配置）。"""
    try:
        return path.resolve().is_relative_to(Path.cwd().resolve())
    except Exception:  # noqa: BLE001 - 诊断用途，失败即视为无告警
        return False
