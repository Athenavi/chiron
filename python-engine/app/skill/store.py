"""Skill Store（磁盘持久化，兼容 Go SkillStore 语义）。

存储目录下每个 `.skill.json` 文件对应一个 SkillDef。

## 多租户 / 用户级隔离 + 租户共享层（四级目录解析）

- 目录解析规则（`SkillStore(root=...)` 显式传入优先）：
  1. 显式 `root` → 直接使用，不做身份解析（兼容旧调用方 / 测试注入）；
  2. 未传 root 时，base = `SKILL_STORE_PATH` 环境变量或 `data/skills`：
     - 同时缺失 tenant_id/user_id（未登录 / 系统任务）→ `base/_shared/`（全局共享）；
     - 有 tenant_id（无 user_id）→ `base/{tenant_id}/_shared/`（租户共享层）；
     - 有 user_id → `base/{tenant_id}/{user_id}/`（用户私有层，tenant 缺失则 `base/{user_id}/`）。
- 四级完整解析（读 / 查找技能）：
  `base/{tenant_id}/{user_id}/` → `base/{tenant_id}/_shared/` → `base/_shared/`，
  按此顺序找第一个命中（user 私有 > 租户共享 > 全局共享）。
- 写（install/register/save）：默认 user 私有目录；显式 `root` 或指定
  `scope='tenant'` 时写租户共享层 `base/{tenant_id}/_shared/`（保留显式 root 兼容）。
- 身份段（tenant_id / user_id）只允许 `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`，
  其余一律拒绝（`ValueError`），防 `../` 路径穿越导致跨租户目录逃逸。
- 兼容迁移（幂等，线程锁保护）：
  - 全局：首次访问 `base/_shared/` 时，若其为空但旧全局目录（base 本身）
    存在 `.skill.json`，将旧目录内容复制进 `_shared`（只复制不移动）。
  - 租户：首次访问 `base/{tid}/_shared/` 时，若其为空但旧租户根
    `base/{tid}/` 存在 `.skill.json`（第 4 轮之前 tenant 无 user 的写入位置），
    将根目录下的技能文件复制进租户 `_shared`（只复制顶层 `.skill.json`，
    不复制子目录，避免把 user 目录误并入共享层）。

## 双载体读取（D1）与内置技能源（D3）

- 每个搜索目录里两种技能载体都认：`{name}.skill.json`（Chiron 原生扁平定义）与
  `{name}/SKILL.md`（deepagents 兼容的目录型技能，解析见 app/skill/skillmd.py）。
  同一目录内 `.skill.json` 先产出，命名冲突时 json 胜出（first-wins）。任一载体非法
  （坏 JSON / 坏 frontmatter）都跳过并 `logger.warning`，不静默。
- 搜索路径**末尾**追加内置技能源 `market/skills`（scope=`builtin`，对位 deepagents 的
  `built_in_skills`）。first-wins + 高优先在前 ⇒ 用户同名技能覆盖内置。

API 层（app/api/skills.py）与运行时工具链（app/tools/skill.py）共用此解析规则：
API 从 query 参数（网关注入）取身份，工具链从 app.tools.context（contextvars）取身份。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 共享目录名（无身份时的回退目录）
SHARED_DIR = "_shared"
# 默认技能根（环境变量 SKILL_STORE_PATH 可覆盖 base）
DEFAULT_SKILL_ROOT = os.path.join(".", "data", "skills")
# 身份段白名单：防路径穿越（不允许 / \ .. 空格等）
_IDENTITY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

logger = logging.getLogger(__name__)

_migration_lock = threading.Lock()

PathLike = str | Path

# 技能来源标记（scope）：user=用户私有目录 / tenant=租户共享层 / shared=全局共享层 /
# root=显式 root（测试注入与旧调用方）/ builtin=内置技能源（market/skills）
SCOPE_USER = "user"
SCOPE_TENANT = "tenant"
SCOPE_SHARED = "shared"
SCOPE_ROOT = "root"
SCOPE_BUILTIN = "builtin"


def _safe_segment(value: str, label: str) -> str:
    """校验并规范化身份段；非法输入抛 ValueError（防路径穿越）。"""
    v = str(value or "").strip()
    if not v:
        return ""
    if not _IDENTITY_SEGMENT_RE.match(v):
        raise ValueError(f"invalid {label} segment: {value!r}")
    return v


def _migrate_legacy_to_shared(base: Path, shared: Path) -> None:
    """兼容迁移（全局）：_shared 为空但旧全局目录有技能时，把旧目录内容复制进 _shared。

    只复制不移动（非破坏性）；幂等（_shared 非空即跳过）；线程锁防并发重复复制。
    """
    if not base.is_dir():
        return
    with _migration_lock:
        if any(shared.glob("*.skill.json")):
            return
        legacy_files = [p for p in base.glob("*.skill.json") if p.is_file()]
        if not legacy_files:
            return
        shared.mkdir(parents=True, exist_ok=True)
        for p in legacy_files:
            shutil.copy2(p, shared / p.name)
        # 旧目录里可能的子目录一并复制（排除 _shared 自身，防递归）
        for d in base.iterdir():
            if d.is_dir() and d.name != SHARED_DIR:
                shutil.copytree(d, shared / d.name, dirs_exist_ok=True)


def _migrate_tenant_legacy_to_shared(tenant_root: Path, shared: Path) -> None:
    """兼容迁移（租户层）：租户根 `base/{tid}/` 下的旧技能文件复制进租户 `_shared`。

    第 4 轮之前 tenant 无 user 时技能直接写在租户根目录；本次升级后租户根被
    `_shared` 取代，首次访问时把根目录顶层 `.skill.json` 复制进共享层。
    只复制文件、不复制子目录（避免把 user 私有目录误并入共享层）；幂等 + 线程锁。
    """
    if not tenant_root.is_dir():
        return
    with _migration_lock:
        if any(shared.glob("*.skill.json")):
            return
        legacy_files = [p for p in tenant_root.glob("*.skill.json") if p.is_file()]
        if not legacy_files:
            return
        shared.mkdir(parents=True, exist_ok=True)
        for p in legacy_files:
            shutil.copy2(p, shared / p.name)


@dataclass(frozen=True)
class SkillRoots:
    """目录解析结果：写目标 + 读搜索路径（按优先级）+ 写目标来源标记。

    - `write`：当前 store 的写目录（save/delete 的目标）。
    - `search`：读查找路径，元素为 `(目录, scope)`，按优先级从高到低；
      `list()` 沿此顺序合并去重，`get()` 取第一个命中。
    - `write_scope`：写目录对应的来源标记（user/tenant/shared/root；builtin 只出现在
      `search` 中，不作为写目标）。
    """

    write: Path
    search: tuple[tuple[Path, str], ...]
    write_scope: str


def _with_builtin_source(
    search: list[tuple[Path, str]],
) -> list[tuple[Path, str]]:
    """在搜索路径**末尾**追加内置技能源（`market/skills`，最低优先级）。

    对位 deepagents 的 `built_in_skills`（方案 02 D3）。`search` 高优先级在前，而
    `_iter_search` 是 first-wins，因此**用户同名技能覆盖内置** —— 等价于 deepagents
    的 last-wins（其加载顺序是 built_in 先、用户后），见评审 01 §2.5。

    内置目录缺失时静默跳过：只是「没有内置技能」，不该让技能列表报错。
    """
    # 延迟导入：skillmd 顶层依赖 store.SkillDef，顶层互相 import 会成环
    from app.skill.skillmd import builtin_skills_root

    builtin = builtin_skills_root()
    if builtin is not None:
        search.append((builtin, SCOPE_BUILTIN))
    return search


def resolve_skill_roots(
    root: PathLike | None = None,
    *,
    tenant_id: str = "",
    user_id: str = "",
    scope: str = "",
) -> SkillRoots:
    """解析技能存储目录（四级：显式 root / 用户私有 / 租户共享 / 全局共享）。

    显式 root 优先；否则按身份解析（见模块 docstring）。身份段非法时抛 ValueError。
    `scope='tenant'` 时写目标为租户共享层（需 tenant_id）；`scope='private'` / 空
    时写目标为用户私有目录（有 user_id）或租户共享层（仅 tenant_id）。
    """
    if root:
        return SkillRoots(
            write=Path(root), search=((Path(root), SCOPE_ROOT),), write_scope=SCOPE_ROOT
        )
    base = Path(os.getenv("SKILL_STORE_PATH", DEFAULT_SKILL_ROOT))
    tid = _safe_segment(tenant_id, "tenant_id")
    uid = _safe_segment(user_id, "user_id")
    global_shared = base / SHARED_DIR
    # 全局旧目录迁移（无身份路径也会触发，与历史行为一致）
    _migrate_legacy_to_shared(base, global_shared)
    if not tid and not uid:
        # 身份缺失（未登录 / 系统任务）→ 全局共享目录（末尾再挂内置源）
        return SkillRoots(
            write=global_shared,
            search=tuple(_with_builtin_source([(global_shared, SCOPE_SHARED)])),
            write_scope=SCOPE_SHARED,
        )

    if scope == "tenant" and not tid:
        raise ValueError("scope='tenant' requires a tenant_id")

    tenant_shared = base / tid / SHARED_DIR
    # 租户根目录旧技能迁移进租户共享层（幂等）
    _migrate_tenant_legacy_to_shared(base / tid, tenant_shared)

    search: list[tuple[Path, str]] = []
    if uid:
        search.append((base / tid / uid, SCOPE_USER))
    search.append((tenant_shared, SCOPE_TENANT))
    search.append((global_shared, SCOPE_SHARED))
    # D3：内置技能源作为**最低优先级**源追加到末尾（用户同名技能覆盖内置）
    _with_builtin_source(search)

    if scope == "tenant":
        write, write_scope = tenant_shared, SCOPE_TENANT
    elif uid:
        write, write_scope = base / tid / uid, SCOPE_USER
    else:
        # 仅 tenant_id（系统级操作）→ 写入租户共享层
        write, write_scope = tenant_shared, SCOPE_TENANT
    return SkillRoots(write=write, search=tuple(search), write_scope=write_scope)


def resolve_skill_root(
    root: PathLike | None = None,
    *,
    tenant_id: str = "",
    user_id: str = "",
) -> Path:
    """兼容包装：返回解析结果中的写目录（历史签名，外部仍可按需调用）。"""
    return resolve_skill_roots(root, tenant_id=tenant_id, user_id=user_id).write


@dataclass
class SkillDef:
    name: str
    description: str
    version: str = "0.1.0"
    author: str = ""
    tags: list[str] = field(default_factory=list)
    exec_type: str = "prompt"
    source: str = ""
    parameters: list[dict[str, Any]] = field(default_factory=list)
    installed_at: float = field(default_factory=time.time)
    enabled: bool = True
    # D6 元数据补齐：对齐 deepagents 的 SKILL.md frontmatter。
    # license / compatibility / allowed_tools / metadata 由目录型技能（SKILL.md）填充，
    # 扁平 `.skill.json` 也可带同名键（见 _load），两者最终经 to_dict 暴露给 skill_list。
    license: str = ""
    compatibility: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # D1 目录附件：目录型技能同目录下除 SKILL.md 外的文件（相对路径）与来源目录，
    # 供渐进式披露（D2 的「可读路径」）与按需读正文；扁平技能为空。
    # `source_dir` 是**服务器绝对路径**，刻意不写入 to_dict（避免经 /v1/skills 泄漏给前端）。
    attachments: list[str] = field(default_factory=list)
    source_dir: str = ""
    # 运行时派生的来源标记（user/tenant/shared/root/builtin），不参与磁盘持久化语义；
    # list()/get() 按目录命中位置填充，供 API 层映射为返回结构里的 source 字段。
    scope: str = SCOPE_USER

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "tags": self.tags,
            "exec": {"type": self.exec_type, "source": self.source},
            "parameters": self.parameters,
            "installed_at": self.installed_at,
            "enabled": self.enabled,
            "license": self.license,
            "compatibility": self.compatibility,
            "allowed_tools": self.allowed_tools,
            "metadata": self.metadata,
            "attachments": self.attachments,
        }


class SkillStore:
    def __init__(
        self,
        root: PathLike | None = None,
        *,
        tenant_id: str = "",
        user_id: str = "",
        scope: str = "",
    ) -> None:
        """按身份解析存储目录；显式 root 优先（见 resolve_skill_roots）。

        scope 仅影响写目标：'tenant' → 租户共享层；空 / 'private' → 用户私有目录
        （无 user_id 时回退租户共享层）。读取始终沿 user → 租户 shared → 全局 shared。
        """
        self._roots = resolve_skill_roots(
            root, tenant_id=tenant_id, user_id=user_id, scope=scope
        )
        self._root = self._roots.write
        self._root.mkdir(parents=True, exist_ok=True)
        #: D5：最近一次遍历遇到的载入问题（坏 JSON / 坏 frontmatter）。
        #: 收集它是为了让**模型**也能看到"某技能加载失败" —— 否则它只是日志里的一行，
        #: 表现是"我明明放了技能却不生效"（`_iter_search` 的注释已自陈该风险）。
        self._warnings: list[str] = []

    @property
    def last_warnings(self) -> list[str]:
        """最近一次 `list()` / `get()` 期间的载入告警（供 `<skill_load_warnings>` 注入）。"""
        return list(self._warnings)

    @property
    def root(self) -> Path:
        """当前 store 的物理写根目录（显式 root 或按身份解析后的写目录）。"""
        return self._root

    @property
    def write_scope(self) -> str:
        """写目录的来源标记（user/tenant/shared/root），供 API 返回 source 字段。"""
        return self._roots.write_scope

    def _path(self, name: str) -> Path:
        return self._root / f"{name}.skill.json"

    def _iter_search(self) -> Iterator[tuple[SkillDef, str]]:
        """按优先级遍历搜索路径，产出 (SkillDef, scope)。

        每个搜索目录里两种载体都认：`{name}.skill.json`（Chiron 原生扁平定义）与
        `{name}/SKILL.md`（deepagents 兼容的目录型技能，见 app/skill/skillmd.py）。
        同一目录内 `.skill.json` 先于 SKILL.md 产出，故命名冲突时 json 胜出（first-wins）。

        任一载体非法（坏 JSON / 坏 frontmatter）都**跳过并告警** —— 不让坏技能静默消失
        （静默会让「我明明放了技能却不生效」无从排查）。
        """
        # 延迟导入：skillmd 顶层依赖本模块的 SkillDef，顶层互相 import 会成环
        from app.skill.skillmd import iter_skill_dirs, load_skill_dir

        for d, scope in self._roots.search:
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.skill.json")):
                try:
                    yield self._load(p), scope
                except Exception as e:  # noqa: BLE001 — 单个坏文件不该拖垮整个列表
                    logger.warning("skill store: skip invalid skill json %s: %s", p, e)
                    self._warnings.append(f"{p.name}: {str(e)[:200]}")
            for skill_dir in iter_skill_dirs(d):
                try:
                    yield load_skill_dir(skill_dir), scope
                except Exception as e:  # noqa: BLE001 — 同上
                    logger.warning(
                        "skill store: skip invalid SKILL.md dir %s: %s", skill_dir, e
                    )
                    self._warnings.append(f"{skill_dir.name}/SKILL.md: {str(e)[:200]}")

    def list(self) -> list[SkillDef]:
        """合并 user 目录 + 租户 _shared + 全局 _shared（去重，user 优先）。

        每个返回的 SkillDef 带 `scope` 标记（user/tenant/shared），供上层标注
        “团队共享”徽标等展示用途。同名技能取搜索路径中优先级最高的那份。
        """
        # 每次重新收集：告警属于"这一次加载"，不是历史累积
        self._warnings = []
        seen: dict[str, SkillDef] = {}
        for skill, scope in self._iter_search():
            if skill.name not in seen:
                skill.scope = scope
                seen[skill.name] = skill
        return [seen[k] for k in sorted(seen)]

    def get(self, name: str) -> SkillDef | None:
        """按 user → 租户 _shared → 全局 _shared 顺序找第一个命中并标记 scope。"""
        for skill, scope in self._iter_search():
            if skill.name == name:
                skill.scope = scope
                return skill
        return None

    def save(self, skill: SkillDef) -> None:
        """写入写目标目录：默认 user 私有目录；scope='tenant' 时写租户共享层。

        注意：保存的 SkillDef 若来自共享层命中（如 toggle 回写），会落盘到写目录
        形成“本地覆盖”，读取时本地版本优先——团队共享技能可被成员本地停用。
        """
        p = self._path(skill.name)
        p.write_text(
            json.dumps(skill.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_skill_md(self, name: str, text: str) -> Path:
        """写**目录型**技能（`{root}/{name}/SKILL.md`）—— `skill_generate`（D4）用。

        与 `save()`（扁平 `.skill.json`）并列：D1 之后两种载体都**读**，写的时候按调用方
        要的形态写。这里只做**路径安全**校验（名字不能含路径分隔符 / `..`）；**内容校验不在
        这里** —— 调用方写完用 `load_skill_dir()` 复查，不通过就回滚（见 `app/skill/generate.py`）。
        """
        safe = (name or "").strip()
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
        if (
            not safe
            or len(safe) > 64
            or not set(safe) <= allowed
            or safe.startswith(".")
            or safe.startswith("-")
        ):
            raise ValueError(f"invalid skill name: {name!r}")
        target = self._root / safe
        target.mkdir(parents=True, exist_ok=True)
        path = target / "SKILL.md"
        path.write_text(text, encoding="utf-8")
        return path

    def delete_skill_dir(self, name: str) -> bool:
        """删除**目录型**技能（D4 的失败回滚：宁可什么都不留，也不留半个坏技能）。"""
        safe = (name or "").strip().strip("/")
        if not safe or any(ch in safe for ch in "/\\") or safe in (".", ".."):
            return False
        target = self._root / safe
        if not target.is_dir():
            return False
        for item in sorted(target.rglob("*"), reverse=True):
            try:
                item.unlink() if item.is_file() else item.rmdir()
            except OSError:
                return False
        try:
            target.rmdir()
        except OSError:
            return False
        return True

    def delete(self, name: str) -> bool:
        """仅删除写目标目录（默认 user 私有目录）中的技能。

        租户共享层技能的删除由租户 owner 后续通过 scope=tenant 单独处理；
        当前版本简化：API 层 DELETE 不带 scope（见 app/api/skills.py），因此
        实际只会删除用户私有目录下的技能，租户/全局共享技能不受影响。
        """
        p = self._path(name)
        if p.exists():
            p.unlink()
            return True
        return False

    def _load(self, p: Path) -> SkillDef:
        data = json.loads(p.read_text(encoding="utf-8"))
        exec_cfg = data.get("exec", {})
        return SkillDef(
            name=data["name"],
            description=data.get("description", ""),
            version=data.get("version", "0.1.0"),
            author=data.get("author", ""),
            tags=data.get("tags", []),
            exec_type=exec_cfg.get("type", "prompt"),
            source=exec_cfg.get("source", ""),
            parameters=data.get("parameters", []),
            installed_at=data.get("installed_at", 0),
            enabled=data.get("enabled", True),
            license=data.get("license", ""),
            compatibility=data.get("compatibility", ""),
            allowed_tools=data.get("allowed_tools", []),
            metadata=data.get("metadata", {}),
            attachments=data.get("attachments", []),
            source_dir=data.get("source_dir", ""),
        )
