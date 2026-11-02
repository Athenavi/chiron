"""压缩淘汰的内容必须可召回。

回归背景：`_prune_messages` 此前只把 **tool** 消息放进 `dropped`，user/assistant
被裁掉就永久消失，上下文里只留一句 "N earlier message(s) were pruned"。

后果不是"上下文变小"，而是正确性问题：用户在会话开头说过的硬约束（"这个仓库
绝不要动 vendor/"）会在中途无声丢失，模型此后无从查证，会重复犯同一个错。

本测试锁死三件事：
1. 被淘汰的 user/assistant 也进 `dropped`（可召回）；
2. 召回块保留**角色身份**（分不清谁说的话，召回就没有意义）；
3. 召回块有大小上限（否则 Redis 被当成归档盘用）。
"""

from __future__ import annotations

from typing import Any

from app.agent.compaction import (
    RECALL_BLOCK_MAX_CHARS,
    CompactionConfig,
    _prune_messages,
    _serialize_for_recall,
)


def _msg(role: str, content: str, **extra: Any) -> dict[str, Any]:
    return {"role": role, "content": content, **extra}


def _conversation(n_pairs: int) -> list[dict[str, Any]]:
    """构造 n 轮 user/assistant 对话（无 tool 消息，专测"非工具消息也会被裁"）。"""
    out: list[dict[str, Any]] = [_msg("system", "你是助手")]
    for i in range(n_pairs):
        out.append(_msg("user", f"问题 {i}"))
        out.append(_msg("assistant", f"回答 {i}"))
    return out


def test_pruned_user_and_assistant_messages_are_recoverable():
    """被裁掉的 user/assistant 必须进 dropped —— 这是"可召回"的全部前提。"""
    messages = _conversation(10)
    dropped: list[tuple[str, str]] = []

    kept = _prune_messages(messages, CompactionConfig(max_messages=6), dropped=dropped)

    assert len(dropped) == 1, "被淘汰的整段应合并成一个召回块"
    ref, block = dropped[0]
    assert ref.startswith("sn_"), "召回 ref 要有稳定前缀，便于识别与排查"
    # 早期约定被裁掉了，它必须出现在召回块里
    assert "问题 0" in block, "被裁掉的用户原话没有进入召回块 → 硬约束会丢失"
    assert "回答 0" in block
    # 保留的近期上下文不应重复进召回块（否则等于没省）
    assert "问题 9" not in block
    assert len(kept) < len(messages)


def test_recall_block_preserves_role_identity():
    """召回块必须能分清谁说的：只存 content 的话读回来无法判断。"""
    block = "\n\n".join(
        _serialize_for_recall(m)
        for m in [
            _msg("user", "绝不要动 vendor/"),
            _msg("assistant", "明白"),
            _msg("tool", "output", tool_call_id="call_1"),
        ]
    )

    assert "## user" in block
    assert "## assistant" in block
    assert "## tool" in block
    assert "call_1" in block, "工具消息要带上 tool_call_id，否则对不上是哪次调用"


def test_recall_block_is_capped():
    """召回块必须有上限：被裁掉的可能是几 MB 的工具输出。"""
    huge = _conversation(1)
    huge.insert(1, _msg("assistant", "x" * (RECALL_BLOCK_MAX_CHARS * 2)))
    for i in range(6):
        huge.append(_msg("user", f"u{i}"))
        huge.append(_msg("assistant", f"a{i}"))

    dropped: list[tuple[str, str]] = []
    _prune_messages(huge, CompactionConfig(max_messages=4), dropped=dropped)

    assert dropped, "应有召回块"
    _, block = dropped[0]
    # 允许少量超出（标记行本身），但绝不允许原样塞进两倍超限的内容
    assert len(block) < RECALL_BLOCK_MAX_CHARS * 1.2, (
        f"召回块 {len(block)} 字符，超出上限 {RECALL_BLOCK_MAX_CHARS} 太多"
    )


def test_prune_note_points_at_the_recall_ref():
    """上下文里那句提示必须带上 ref，否则模型不知道去哪取。"""
    messages = _conversation(10)
    dropped: list[tuple[str, str]] = []

    kept = _prune_messages(messages, CompactionConfig(max_messages=6), dropped=dropped)
    ref, _ = dropped[0]

    note = next(m for m in kept if m["role"] == "user" and "pruned" in m["content"])
    assert ref in note["content"], "提示里没有 ref → 模型拿不到召回入口"
    assert "read_tool_result" in note["content"]


def test_no_dropped_list_keeps_legacy_behaviour():
    """不传 dropped 时不生成 ref：既有调用方行为不变（纯函数、无副作用）。"""
    messages = _conversation(10)

    kept = _prune_messages(messages, CompactionConfig(max_messages=6))

    note = next(m for m in kept if m["role"] == "user" and "pruned" in m["content"])
    assert "sn_" not in note["content"]
    assert "read_tool_result" not in note["content"]


def test_tool_messages_still_get_their_own_refs():
    """工具结果仍各自带 ref（既有契约），与整段召回块并存不冲突。"""
    messages = [
        _msg("system", "sys"),
        _msg("user", "跑个命令"),
        _msg("assistant", "", tool_calls=[{"id": "c1", "name": "shell_exec", "arguments": "{}"}]),
        _msg("tool", "y" * 40_000, tool_call_id="c1"),
        _msg("user", "继续"),
        _msg("assistant", "好"),
    ]
    dropped: list[tuple[str, str]] = []

    _prune_messages(messages, CompactionConfig(max_messages=3), dropped=dropped)

    refs = [ref for ref, _ in dropped]
    assert any(r.startswith("sn_") for r in refs)
    # 工具结果被截断时应保留 tail（既有契约：截断后仍以原文尾巴结尾）
    for ref, text in dropped:
        if "y" * 100 in text and len(text) > 30_000:
            assert text.endswith("y"), f"{ref} 的截断丢了尾部"
