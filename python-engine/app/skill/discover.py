"""技能发现：本地清单与远端索引的**唯一**合并口径。

此前 `skill_discover`（工具链，`app/tools/skill.py`）与 `GET /v1/skills/discover`
（HTTP，`app/api/skills.py`）各写一套发现逻辑，语义还不一致：

* 工具链会合并去重、按上限截断、校验技能名；
* HTTP 侧把远端索引的结果**也塞进 ``local`` 桶**，``remote`` 恒为空数组 ——
  同一个"发现"能力两个入口给出两种真相，接哪个都只能拿到一半。

把判定规则收到这里，两层各自只负责"从哪拿 store"与"怎么序列化"。`SkillStore`
由调用方注入（HTTP 层按 query 里的身份构造，工具层从 contextvars 取），
所以这里不碰身份解析 —— 那是各自入口的职责。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.skill.store import SkillStore

#: 技能名合法形态（与 `skill_install` 的校验同一口径）。
#: 远端索引是**第三方数据**，其中的 name 会进文件路径（`/skills/<name>/SKILL.md`），
#: 不校验就等于把路径注入交给远端决定。
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def local_rows(store: SkillStore) -> dict[str, dict[str, Any]]:
    """本地已安装技能 → ``{name: 统一行}``。远端条目据此标 ``installed``。"""
    rows: dict[str, dict[str, Any]] = {}
    for s in store.list():
        rows[s.name] = {
            "name": s.name,
            "description": s.description,
            "version": s.version,
            "author": s.author,
            "source": s.source,
            # scope 是 user/tenant/shared/builtin，前端据此显示"团队共享"徽标。
            # 与 `source` 不是一回事：source 来自技能定义本身（exec 配置）。
            "scope": s.scope,
            "installed": True,
        }
    return rows


def row_from_registry(
    item: Any, installed: set[str], origin: str = ""
) -> dict[str, Any] | None:
    """远端索引的一条 → 统一行；非对象或 name 不合法则丢弃。

    丢弃而非抛错：一个索引里混进几条坏数据是常态，不该让整次发现失败。
    """
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not SKILL_NAME_RE.match(name):
        return None
    return {
        "name": name,
        "description": str(item.get("description") or ""),
        "version": str(item.get("version") or ""),
        "author": str(item.get("author") or ""),
        # 条目自带 source 优先；没有则回落到索引地址，便于前端回溯来源。
        "source": str(item.get("source") or origin),
        "scope": "",
        "installed": name in installed,
    }


@dataclass(slots=True)
class DiscoverResult:
    """合并结果。``rows`` 里本地在前（已装的更相关），``installed`` 区分来源。"""

    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    local_count: int = 0


def merge_registry(
    store: SkillStore, payload: list[Any], *, limit: int, origin: str = ""
) -> DiscoverResult:
    """把远端索引合并进本地清单：本地优先、同名去重、按上限截断。

    ``limit`` 约束的是**总条数**（含本地）：远端索引由第三方提供，可能几千条，
    全量渲染进工具返回值会直接挤爆上下文。被截断的条数写进 warnings，
    让模型知道"还有更多"，而不是以为这就是全部。
    """
    local = local_rows(store)
    installed = set(local)
    rows = list(local.values())
    seen = set(local)
    omitted = 0

    for item in payload:
        row = row_from_registry(item, installed, origin)
        if row is None or row["name"] in seen:
            continue
        if len(rows) >= limit:
            omitted += 1
            continue
        rows.append(row)
        seen.add(row["name"])

    warnings: list[str] = []
    if omitted:
        warnings.append(f"{omitted} more remote entries omitted (limit {limit})")
    return DiscoverResult(rows=rows, warnings=warnings, local_count=len(local))


def render_lines(rows: list[dict[str, Any]]) -> str:
    """把结果渲染成给模型看的单行摘要（工具返回值用）。"""
    return "\n".join(
        f"  [{'✓' if r['installed'] else ' '}] {r['name']}: {r['description']}"
        f" (v{r['version']})"
        for r in rows
    )
