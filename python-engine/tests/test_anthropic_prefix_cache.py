"""P1-① / 切口 2a：Anthropic 侧的显式前缀缓存断点（对位 deepagents 的 `_prompt_caching`）。

**这个文件钉住的缺陷（修复前为红）**

`_apply_system_prefix`（`app/agent/runtime.py`）产出的 system 段是**多条**消息：
「稳定前缀（`cache_breakpoint=True`）→ 记忆 → hook 上下文」，设计上是"断点之后的内容
每轮可变，而前缀缓存仍命中"（见 `tests/test_memory_injection_block.py`）。

但 `AnthropicProvider._convert_messages` 用 `system = msg.content` **覆盖**取值 ⇒ 只剩
**最后一条** system，**稳定前缀被整条丢弃**：

* 正确性：注入了记忆时，system prompt 前缀根本没发给模型；
* 缓存：断点标在前缀上，而前缀没发出 ⇒ `cache_control` 无处可落，
  `cached_tokens` 恒为 0（全价重算）。

修复口径：按序保留全部 system 块；**仅当存在断点**时改用 block 数组并打
`{"type": "ephemeral"}`，否则逐字保持原有字符串形态（默认行为不变）。
"""

from __future__ import annotations

from app.gateway.provider import ChatMessage
from app.providers.anthropic import AnthropicProvider

convert = AnthropicProvider._convert_messages


def _prefix_and_memory() -> list[ChatMessage]:
    """`_apply_system_prefix` 的真实形态：前缀带断点 + 记忆无断点。"""
    return [
        ChatMessage(role="system", content="PREFIX", cache_breakpoint=True),
        ChatMessage(role="system", content="MEMORY"),
        ChatMessage(role="user", content="hi"),
    ]


def test_stable_prefix_is_not_dropped_by_memory():
    """反例：修复前 `system == "MEMORY"`，前缀消失。"""
    system, _msgs = convert(_prefix_and_memory())

    assert "PREFIX" in str(system), "稳定前缀被后续 system 消息覆盖了"


def test_all_system_blocks_keep_their_order():
    system, _msgs = convert(_prefix_and_memory())

    text = str(system)
    assert text.index("PREFIX") < text.index("MEMORY"), "前缀必须仍在记忆之前"


def test_breakpoint_becomes_cache_control_block():
    system, _msgs = convert(_prefix_and_memory())

    assert isinstance(system, list), "有断点时 system 应为 block 数组（Anthropic 的块级 cache_control）"
    assert system[0].get("cache_control") == {"type": "ephemeral"}, "断点应落在**稳定前缀**上"
    assert "cache_control" not in system[1], "记忆每轮可变，不该是断点"


def test_no_breakpoint_keeps_plain_string():
    """默认关：没有断点时逐字保持原状（不得因本次改动而变化）。"""
    system, _msgs = convert([ChatMessage(role="system", content="S")])

    assert system == "S", "未标记断点的调用方行为必须逐字不变"


def test_user_and_tool_messages_are_untouched():
    """只改 system 段：user/tool/assistant 的转换结果不变。"""
    _system, msgs = convert(
        [
            ChatMessage(role="system", content="PREFIX", cache_breakpoint=True),
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="tool", content="r", tool_call_id="c1"),
        ]
    )

    assert [m["role"] for m in msgs] == ["user", "user"]
    assert msgs[1]["content"][0]["tool_use_id"] == "c1"
