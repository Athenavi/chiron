"""推理参数的**按后端契约表**（D2）。

## 为什么需要它

`app/providers/effort.py` 解决的是"**值**怎么归一"（各家写 `off` / `none` / `false`…），
但没解决"**参数放哪**" —— 而后者同样会 400：

* OpenAI 兼容（chat/completions）：`reasoning_effort: "low"|"medium"|"high"|"minimal"`（顶层字段）；
* OpenAI Responses（/v1/responses）：`reasoning: {"effort": ..., "summary": ...}`（**子对象**）；
* Anthropic：`thinking: {"type": "enabled", "budget_tokens": N}`（**预算**而非档位）；
* DeepSeek 直连：思考是模型固有行为，**没有**档位参数 —— 发任何字段都可能被拒。

只归一值、不看"放哪"，就会把 `reasoning_effort` 发给 Anthropic、把 `thinking` 发给 OpenAI。
本模块把这个映射写成**显式契约表**：每个后端一行，说明参数位置与取值，并给出"不要发送"的
明确表达（`None`）。

## 与 effort.py 的关系

值是同源问题，因此**复用** `effort.py` 的归一化（不新造一份词表 —— 两份必然漂移）。
本模块只决定"归一后的值该放到请求的哪个位置"。

## 纪律

* **拿不准就不发**：`resolve()` 在无匹配契约或档位为空时返回空 dict，退化为 provider 默认。
* **契约是数据不是分支**：新增后端 = 加一行 `ReasoningContract`，而不是加一个 `if`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from types import MappingProxyType
from typing import Any

from app.providers.effort import normalize_reasoning_effort

#: 契约风格：参数放哪。
STYLE_TOP_LEVEL = "top-level"  # 请求顶层字段（OpenAI 兼容）
STYLE_NESTED = "nested"  # 放进一个子对象（Responses / Anthropic）
STYLE_NONE = "none"  # 不接受任何档位参数


@dataclass(frozen=True)
class ReasoningContract:
    """一个后端（按 provider / 模型前缀匹配）的推理参数契约。"""

    #: 匹配键：provider 名或模型名前缀（小写）。
    prefixes: tuple[str, ...]
    style: str
    #: 顶层字段名（style=top-level）或子对象名（style=nested）。
    field: str = "reasoning_effort"
    #: 子对象里放档位的键（style=nested 时用）。
    nested_key: str = "effort"
    #: 档位 → 该后端接受的写法。缺省的档位按"不发"处理。
    #: ⚠ 必须用 `default_factory`：Python 3.11 的 dataclasses 拒绝 `mappingproxy` 这类
    #: **mutable default**（报 `ValueError: mutable default <class 'mappingproxy'>`），
    #: 3.12+ 放宽了 ⇒ 本机（3.14）绿、CI（3.11，`requires-python >= 3.11` 的下限）红。
    #: ⚠ 别名导入是必须的：本类**有一个字段就叫 `field`**（见上），而类体里 `field` 会先被
    #: 那个字段的默认值绑定 ⇒ 直接用 `field(...)` 会报 `'str' object is not callable`。
    levels: Mapping[str, str] = dc_field(default_factory=lambda: MappingProxyType({}))
    #: 说明（供诊断页/文档展示；也是"为什么这样配"的落点）。
    notes: str = ""


#: 契约表。**顺序无关**（按最长前缀匹配），但同一后端只应有一行。
_CONTRACTS: tuple[ReasoningContract, ...] = (
    ReasoningContract(
        prefixes=("openai", "o1", "o3", "o4", "gpt-5"),
        style=STYLE_TOP_LEVEL,
        field="reasoning_effort",
        levels=MappingProxyType({"minimal": "minimal", "low": "low", "medium": "medium", "high": "high"}),
        notes="OpenAI 兼容 chat/completions：顶层 reasoning_effort，无 max。",
    ),
    ReasoningContract(
        prefixes=("openai-responses", "responses"),
        style=STYLE_NESTED,
        field="reasoning",
        nested_key="effort",
        levels=MappingProxyType({"minimal": "minimal", "low": "low", "medium": "medium", "high": "high"}),
        notes="OpenAI Responses：reasoning 子对象（可另带 summary）。",
    ),
    ReasoningContract(
        prefixes=("anthropic", "claude"),
        style=STYLE_NESTED,
        field="thinking",
        nested_key="type",
        levels=MappingProxyType({"low": "enabled", "medium": "enabled", "high": "enabled"}),
        notes="Anthropic 用 thinking 开关 + budget_tokens 预算，而不是档位；"
        "档位只决定开不开，预算由调用方另行给。",
    ),
    ReasoningContract(
        prefixes=("deepseek",),
        style=STYLE_NONE,
        notes="DeepSeek 直连：思考是模型固有行为，没有档位参数 —— 发任何字段都可能被拒。",
    ),
)


def resolve_contract(provider: str = "", model: str = "") -> ReasoningContract | None:
    """按键（provider 优先，其次模型名）找契约；**最长前缀优先**，无匹配返回 None。

    provider 优先于模型名：同名模型经不同网关接入时，决定参数形态的是**网关协议**，不是模型名。
    """
    keys = [k.strip().lower() for k in (provider, model) if k and k.strip()]
    if not keys:
        return None
    best: ReasoningContract | None = None
    best_len = 0
    for contract in _CONTRACTS:
        for prefix in contract.prefixes:
            p = prefix.strip().lower()
            if not p:
                continue
            for key in keys:
                if key.startswith(p) and len(p) > best_len:
                    best, best_len = contract, len(p)
    return best


def resolve(effort: Any, *, provider: str = "", model: str = "") -> dict[str, Any]:
    """把档位解成**要并入请求的字段**（`{}` = 什么都不发）。

    这是本模块的主入口：
      * 无契约 / 档位归一后为空 ⇒ `{}`（退化为 provider 默认，**绝不发对方不认的值**）；
      * `style=none` 的契约 ⇒ `{}`（后端不接受档位参数）。
    """
    contract = resolve_contract(provider, model)
    if contract is None or contract.style == STYLE_NONE:
        return {}

    level = normalize_reasoning_effort(effort)
    if not level:
        return {}
    mapped = contract.levels.get(level)
    if not mapped:
        # 该后端没有对应档位（例如 Anthropic 只区分开/关）：**不发**，而不是硬塞一个近似值。
        return {}

    if contract.style == STYLE_TOP_LEVEL:
        return {contract.field: mapped}
    if contract.style == STYLE_NESTED:
        return {contract.field: {contract.nested_key: mapped}}
    return {}


def describe() -> list[dict[str, Any]]:
    """契约表的可展示形式（诊断页 / 文档用；避免各处再抄一份）。"""
    out: list[dict[str, Any]] = []
    for c in _CONTRACTS:
        out.append({
            "prefixes": list(c.prefixes),
            "style": c.style,
            "field": c.field if c.style != STYLE_NONE else "",
            "nested_key": c.nested_key if c.style == STYLE_NESTED else "",
            "levels": dict(c.levels),
            "notes": c.notes,
        })
    return out
