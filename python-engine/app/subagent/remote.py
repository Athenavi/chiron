"""S3：远端 / 跨服务子 agent（方案 01 §4.3）—— 把子 agent 派到**另一个引擎实例**执行。

三个环节都**复用既有机制**，不新造协议栈：

| 环节 | 复用 |
|---|---|
| 发现 | 引擎注册表 `engine:instance:*`（`app/engine_registry.py` 写、Go 的 `discovery.go` 也读它）|
| 认证 | 内网 `X-Internal-Token`（与网关代理路径同一凭据）；接收端走**body-身份端点**形态 |
| 结果 | 仍是 `<subagent-result>` **不可信包裹** —— 不得因"远端化"而丢失 |

**安全前置**（方案 §4.3 的硬要求，逐条落地）：

1. 跨实例请求带租户与用户身份，**接收方重新校验**（拒绝空身份；不信任请求体里任何"已授权"标记）；
2. 结果回流仍按不可信数据包裹 —— 远端已包就原样透传，**未包则本侧补包**（两道保证）；
3. **默认关**（`remote_subagent_enabled`）：跨实例执行扩大信任边界，不该是默认行为；
4. 远端不可达 / 返回异常 ⇒ **回退本实例**（`ctx.run_child`），不把一次委派变成一次失败。
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 远端调用的 HTTP 超时（秒）。与同步委派的 wall 上限同量级。
DEFAULT_REMOTE_TIMEOUT = 300.0

#: 内网端点（接收端在 `app/api/internal_subagent.py` 实现）
REMOTE_RUN_PATH = "/v1/internal/subagent/run"


@dataclass
class RemoteResult:
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    engine: str = ""


async def list_remote_engines(*, exclude: str = "") -> list[dict[str, str]]:
    """列出**其它**引擎实例（`[{instance_id, url}]`）。

    直接读引擎注册表 —— 与 Go 的 `discovery.go` 是**同一份数据**（同一 Redis 前缀、同一 JSON
    结构），因此不引入第二套发现机制，也不会与网关看到的实例集合不一致。
    """
    try:
        from app.redis_client import get_redis
        from app.redis_keys import rkey

        redis = await get_redis()
        if redis is None:
            return []

        prefix = rkey("engine:instance:")
        found: list[dict[str, str]] = []
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=f"{prefix}*", count=100)
            for key in keys or []:
                name = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
                instance_id = name[len(prefix) :] if name.startswith(prefix) else name
                if not instance_id or instance_id == exclude:
                    continue
                raw = await redis.get(key)
                if not raw:
                    continue
                try:
                    data = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
                except (TypeError, ValueError):
                    continue
                url = str((data or {}).get("url") or "")
                if url:
                    found.append({"instance_id": instance_id, "url": url})
            if cursor == 0:
                break
        return found
    except Exception as exc:  # noqa: BLE001 — 发现失败按"没有远端"处理（调用方会回退本实例）
        logger.warning("remote engine discovery failed: %s", exc)
        return []


async def resolve_engine(ref: str) -> dict[str, str] | None:
    """把 `remote:<ref>` 的 ref 解析成一个具体引擎。

    `auto`（或缺省）随机挑一个其它实例 —— 随机而不是固定取第一个：多副本下固定取第一个会让
    负载永远压在同一个实例上。指定 instance_id 时精确匹配（不存在则返回 None，由调用方回退）。
    """
    exclude = ""
    try:
        from app.subagent.affinity import cached_instance_id

        exclude = cached_instance_id() or ""
    except Exception:  # noqa: BLE001
        exclude = ""

    engines = await list_remote_engines(exclude=exclude)
    if not engines:
        return None
    wanted = (ref or "auto").strip()
    if wanted in ("", "auto"):
        return random.choice(engines)
    for engine in engines:
        if engine["instance_id"] == wanted:
            return engine
    logger.info("remote engine %r not found among %d live instance(s)", wanted, len(engines))
    return None


async def run_remote(
    *,
    engine: dict[str, str],
    task: str,
    tenant_id: str,
    user_id: str,
    session_id: str = "",
    mode: str = "normal",
    max_turns: int = 5,
    allow_write: bool = False,
    depth: int = 0,
    timeout: float = DEFAULT_REMOTE_TIMEOUT,
) -> RemoteResult:
    """把一次委派发给远端引擎；**任何失败都返回 `ok=False`**（由调用方决定回退）。"""
    instance_id = str(engine.get("instance_id") or "")
    base_url = str(engine.get("url") or "").rstrip("/")
    if not base_url:
        return RemoteResult(ok=False, error="remote engine has no url", engine=instance_id)

    try:
        from app.config import settings
    except Exception:  # noqa: BLE001
        return RemoteResult(ok=False, error="settings unavailable", engine=instance_id)

    if not settings.internal_token:
        # 内网凭据缺失时**不发**请求：既发不出去（会被拒），也不该把无凭据调用当成"试过了"
        return RemoteResult(ok=False, error="internal token not configured", engine=instance_id)

    body = {
        "task": task,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "mode": mode,
        "max_turns": int(max_turns or 0),
        "allow_write": bool(allow_write),
        "depth": int(depth or 0),
    }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url}{REMOTE_RUN_PATH}",
                json=body,
                headers={"X-Internal-Token": settings.internal_token},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 — 远端故障是"可回退"的情形，不是异常
        return RemoteResult(
            ok=False, error=f"remote engine call failed: {str(exc)[:160]}", engine=instance_id
        )

    payload = data.get("data", data) if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return RemoteResult(ok=False, error="remote engine returned a bad payload", engine=instance_id)
    return RemoteResult(ok=True, payload=payload, engine=instance_id)


def _wrap_if_needed(text: str, *, run_id: str, profile: str, status: str) -> str:
    """**保证**离场时结果带 `<subagent-result>` 不可信包裹。

    远端已经包了（正常路径）就原样透传；没包（例如远端换了实现）则本侧补包 —— 这条不变量
    不能因为"结果是从别的服务来的"就打折：父模型看到的每一段子 agent 产物都必须是**数据**。
    """
    if "<subagent-result" in text:
        return text
    from app.agent.subagent_runner import _wrap_result

    return _wrap_result(run_id, profile, status, text, False)


class RemoteSubagentTarget:
    """`target="remote:<instance_id|auto>"` —— 把子 agent 派到另一个引擎实例。

    失败一律**回退本实例**（`ctx.run_child`）：远端不可达是运行时的常态（滚动发布、扩缩容），
    不该让一次委派直接失败。
    """

    prefix = "remote"

    def __init__(self, ref: str) -> None:
        self.ref = ref.strip() or "auto"

    @property
    def name(self) -> str:
        return f"{self.prefix}:{self.ref}"

    @property
    def description(self) -> str:
        return "Run the sub-agent on another engine instance (remote delegation)"

    async def run(self, task: str, ctx: Any) -> Any:
        from app.config import settings
        from app.subagent.registry_targets import failed_result, result_from_payload

        if not getattr(settings, "remote_subagent_enabled", False):
            # 默认关：显式开关才开放跨实例执行（与 hooks / 部署级扩展同一约定）
            return failed_result(
                "remote subagents are disabled (set REMOTE_SUBAGENT_ENABLED=true to enable)"
            )

        engine = await resolve_engine(self.ref)
        if engine is None:
            return await self._fallback(task, ctx, reason="no live remote engine")

        result = await run_remote(
            engine=engine,
            task=task,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            mode=ctx.mode,
            depth=ctx.depth,
        )
        if not result.ok:
            return await self._fallback(task, ctx, reason=result.error)

        payload = dict(result.payload)
        payload["output"] = _wrap_if_needed(
            str(payload.get("output") or ""),
            run_id=str(payload.get("result_ref") or ""),
            profile=self.name,
            status=str(payload.get("status") or "completed"),
        )
        payload.setdefault("profile", self.name)
        return result_from_payload(payload)

    async def _fallback(self, task: str, ctx: Any, *, reason: str) -> Any:
        """远端不可用 → 本实例执行（**不是**失败）。"""
        from app.subagent.registry_targets import failed_result

        if ctx.run_child is None:
            return failed_result(f"remote delegation unavailable ({reason}) and no local fallback")
        logger.info("remote delegation falling back to local child: %s", reason)
        return await ctx.run_child(task)
