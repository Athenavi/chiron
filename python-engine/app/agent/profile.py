"""子 Agent Profile（定义层）—— 复用 ``agents`` 表，``kind='subagent'``。

设计见 ``docs/subagent-design.md`` §3.1。承载列的选择（重要）：

* ``system_prompt`` / ``max_turns`` / ``timeout_seconds`` / ``skills`` / ``plugins`` /
  ``workflows`` / ``kb_id`` / ``visibility`` —— 直接复用既有列；
* ``model`` / ``effort`` 与 Profile 专属项（``allowed_tools`` / ``disallowed_tools`` /
  ``read_only`` / ``max_depth`` / ``output_max_chars`` / ``summary_max_chars`` /
  ``isolation``）—— 放 ``llm_config`` JSON，形如::

      {"model": "deepseek-chat", "effort": "high",
       "subagent": {"read_only": true, "allowed_tools": ["read_file", "grep"]}}

  也兼容 ``llm_config`` 顶层直接写这些键。**不放在 ``tools`` 列**：该列的既有语义是
  "工具定义数组"（Go 网关会把它作为 ``tools`` 透传给引擎），复用会与既有语义冲突。

本模块只做「DB 行 → ProfileSpec」的解析与校验，不涉及执行。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── 默认值（与设计文档 §3.4/§7 一致）──
DEFAULT_MAX_TURNS = 5
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_DEPTH = 1            # 默认禁止子 Agent 再委派（对标 Claude Code 默认不给 Agent 工具）
DEFAULT_OUTPUT_MAX_CHARS = 2000  # L2 回传父上下文的上限
DEFAULT_SUMMARY_MAX_CHARS = 4000  # L1 摘要上限
PROFILE_KIND = "subagent"

LOAD_PROFILE_SQL = """
SELECT id, name, description, system_prompt, llm_config, max_turns, timeout_seconds,
       skills, plugins, workflows, kb_id
  FROM agents
 WHERE kind = $1
   AND (id = $2 OR name = $2)
   AND tenant_id = $3
   AND (user_id = $4 OR user_id IS NULL OR visibility = 'public')
 LIMIT 1
"""


@dataclass(frozen=True)
class ProfileSpec:
    """解析后的 Profile（不可变，供 SubAgentRunner 装配子 Agent）。"""

    id: str = ""
    name: str = ""
    description: str = ""
    system_prompt: str = ""
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    read_only: bool = False
    model: str = ""
    effort: str = ""
    max_turns: int = DEFAULT_MAX_TURNS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_depth: int = DEFAULT_MAX_DEPTH
    output_max_chars: int = DEFAULT_OUTPUT_MAX_CHARS
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS
    isolation: str = "none"
    skills: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()
    kb_id: str = ""

    def llm_config(self) -> dict[str, Any]:
        """转成 ``AgentTask.llm_config``：只在非空时注入，避免覆盖租户/会话默认。"""
        cfg: dict[str, Any] = {}
        if self.model:
            cfg["model"] = self.model
        if self.effort:
            cfg["effort"] = self.effort
        return cfg


# ── 解析 helpers（容错优先：脏数据不应让子 Agent 起不来）──


def _as_tuple(value: Any) -> tuple[str, ...]:
    """JSON 数组 / 逗号串 / None → 字符串元组。"""
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        return ()
    return tuple(v.strip() for v in items if str(v).strip())


def _as_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= minimum else default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return default


def _as_mapping(value: Any) -> dict[str, Any]:
    """JSONB 列（可能是 dict / JSON 字符串 / None）→ dict。"""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def parse_profile_row(row: Mapping[str, Any]) -> ProfileSpec:
    """把 ``agents`` 行解析为 :class:`ProfileSpec`。

    读取顺序：``llm_config["subagent"]`` 子对象 → ``llm_config`` 顶层同名键 → 默认值。
    """
    llm = _as_mapping(row.get("llm_config"))
    sub = _as_mapping(llm.get("subagent"))

    def pick(key: str, default: Any = None) -> Any:
        if key in sub:
            return sub[key]
        return llm.get(key, default)

    return ProfileSpec(
        id=str(row.get("id") or ""),
        name=str(row.get("name") or ""),
        description=str(row.get("description") or ""),
        system_prompt=str(row.get("system_prompt") or ""),
        allowed_tools=_as_tuple(pick("allowed_tools")),
        disallowed_tools=_as_tuple(pick("disallowed_tools")),
        read_only=_as_bool(pick("read_only"), False),
        model=str(pick("model") or ""),
        effort=str(pick("effort") or ""),
        max_turns=_as_int(row.get("max_turns"), DEFAULT_MAX_TURNS),
        timeout_seconds=_as_int(row.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS),
        max_depth=_as_int(pick("max_depth"), DEFAULT_MAX_DEPTH, minimum=0),
        output_max_chars=_as_int(pick("output_max_chars"), DEFAULT_OUTPUT_MAX_CHARS),
        summary_max_chars=_as_int(pick("summary_max_chars"), DEFAULT_SUMMARY_MAX_CHARS),
        isolation=str(pick("isolation") or "none"),
        skills=_as_tuple(row.get("skills")),
        plugins=_as_tuple(row.get("plugins")),
        workflows=_as_tuple(row.get("workflows")),
        kb_id=str(row.get("kb_id") or ""),
    )


async def load_profile(
    pool: Any,
    *,
    ref: str,
    tenant_id: str = "",
    user_id: str = "",
) -> ProfileSpec | None:
    """按 id 或 name 加载 Profile（限 ``kind='subagent'`` 且同租户）。

    ``pool`` 为 ``app.db.get_pool()`` 返回的对象（asyncpg / 统一客户端均可，需支持 ``fetchrow``）。
    查不到 / 表不可用 / 不是 Profile 时返回 ``None``，调用方回退为通用子 Agent。
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    if pool is None:
        logger.debug("load_profile 跳过：无数据库连接池")
        return None
    try:
        row = await pool.fetchrow(
            LOAD_PROFILE_SQL, PROFILE_KIND, ref, tenant_id or "", user_id or ""
        )
    except Exception as exc:  # noqa: BLE001 - Profile 解析失败不应阻断委派
        logger.warning("load_profile 失败（ref=%s）: %s", ref, str(exc)[:200])
        return None
    if not row:
        return None
    spec = parse_profile_row(dict(row))
    if not spec.name:
        spec = ProfileSpec(**{**spec.__dict__, "name": spec.id})
    return spec
