"""按**模型家族**的 harness 塑形（B1，方案 04 §3）。

## 为什么需要它

`app/agent/modes.py` 已经按**运行模式**给 persona（normal / minimal / 创造…），但那是"这次要干
什么"的维度。缺的是另一个**正交**维度：同一个模式跑在不同模型上，需要的行为塑形并不相同 ——
前沿模型的训练偏好不同（是否该主动行动、是否并行调工具、思考该怎么呈现）。

对齐的参照是 deepagents 的 `HarnessProfile`：它的 Codex profile 就是一段行为塑形 suffix
（*autonomous senior engineer / bias to action / parallel tool use / TODO hygiene*），并且**按模型
spec 注册**（不是按 provider 前缀）—— 这样"没注册的模型"行为完全不变。

## 两条硬约束

1. **未注册的模型逐字不变**：`apply_harness_suffix` 没匹配到 profile 时**原样返回**输入。
   这是本模块最重要的性质 —— 引入这个机制不该悄悄改变任何现有模型的输出。
2. **注册表默认为空**：机制就绪，但"给哪个模型加什么 suffix"是产品决定（见方案 04 §8 未决问题）。
   空注册表下 `apply_harness_suffix` 是恒等函数。

## 与 A1 的关系（值得记一笔）

A1 之后，**引擎的 native reasoning 走独立事件**；而 prompt 里仍然写着"用 `[thinking]…[/thinking]`
包住思考"（那是给**没有** reasoning 通道的模型准备的）。将来若要给"支持 reasoning 的模型家族"
注册一条"思考无需正文标记"的 suffix，就该注册在**这里** —— 这也正是本机制存在的意义。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class HarnessProfile:
    """一个模型家族的行为塑形。

    `model_prefixes` 是**匹配前缀**（如 ``"deepseek-"``）。用前缀而不是精确 spec，是为了让同一
    家族的新版本自动继承（deepagents 也说明了这个取舍：只有"训练期望确实分叉"时才拆）。
    """

    name: str
    model_prefixes: tuple[str, ...]
    #: 追加到**所有** system 内容之后（"suffix is appended last"）—— 这样它能盖在调用方与
    #: mode persona 之上，而不会被打断。
    system_prompt_suffix: str = ""
    #: 模型特有的参数覆盖（如 temperature / tool_choice）。形状先定下来：**当前未接线**
    #: —— 参数真正生效需要在 provider 装配处应用，属后续工作（方案 04 §8 未决）。
    params: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


_registry: list[HarnessProfile] = []


def register_harness_profile(profile: HarnessProfile) -> None:
    """注册一个 profile。同名则替换（便于测试与运行时覆盖）。"""
    global _registry
    _registry = [p for p in _registry if p.name != profile.name]
    _registry.append(profile)


def reset_harness_profiles() -> None:
    """清空注册表（测试用）。默认态就是空。"""
    global _registry
    _registry = []


def registered_harness_profiles() -> tuple[HarnessProfile, ...]:
    return tuple(_registry)


def match_harness_profile(model: str) -> HarnessProfile | None:
    """按模型名匹配 profile；无匹配返回 ``None``。

    多个前缀命中时取**最长**的那个 —— 否则 ``"gpt-"`` 会把更具体的 ``"gpt-5-codex"`` 吃掉，
    而"具体覆盖一般"正是这个机制存在的理由。
    """
    name = (model or "").strip().lower()
    if not name:
        return None
    best: HarnessProfile | None = None
    best_len = 0
    for profile in _registry:
        for prefix in profile.model_prefixes:
            p = prefix.strip().lower()
            if p and name.startswith(p) and len(p) > best_len:
                best, best_len = profile, len(p)
    return best


def apply_harness_suffix(system_prompt: str, *, model: str) -> str:
    """给 system 追加该模型家族的 suffix。

    **没匹配到时原样返回** —— 这条是硬约束：机制上线不该改变任何现有模型的行为。
    """
    profile = match_harness_profile(model)
    if profile is None or not profile.system_prompt_suffix:
        return system_prompt
    suffix = profile.system_prompt_suffix
    if not system_prompt:
        return suffix
    return f"{system_prompt}\n\n{suffix}"


__all__ = [
    "HarnessProfile",
    "apply_harness_suffix",
    "match_harness_profile",
    "register_harness_profile",
    "registered_harness_profiles",
    "reset_harness_profiles",
]
