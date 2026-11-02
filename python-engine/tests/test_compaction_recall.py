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


# ── 持久 ref（messages.id）：让 read_archive 真正可用 ──
#
# 背景：`sn_xxx` 存在 Redis `result_store`，TTL 2 小时。会话过夜再回来，
# 裁剪通知里那个 ref 已经过期 —— 模型拿着一条永远取不回的线索。
# `messages.id` 没有 TTL，才是真正的归档入口。


def _conversation_with_ids(n_pairs: int) -> list[dict[str, Any]]:
    """带 `messages.id` 的对话，模拟从 PG 读回来的真实形态。"""
    out: list[dict[str, Any]] = [_msg("system", "你是助手")]
    for i in range(n_pairs):
        out.append(_msg("user", f"问题 {i}", id=f"msg-{i}-u"))
        out.append(_msg("assistant", f"回答 {i}", id=f"msg-{i}-a"))
    return out


def test_prune_note_prefers_durable_message_ids():
    """有 messages.id 时必须指向 `read_archive`，而不是会过期的 Redis ref。"""
    messages = _conversation_with_ids(10)
    dropped: list[tuple[str, str]] = []

    kept = _prune_messages(messages, CompactionConfig(max_messages=6), dropped=dropped)

    note = next(m for m in kept if m["role"] == "user" and "pruned" in m["content"])
    assert "read_archive" in note["content"], "持久 id 可用时不该只给易过期的 Redis ref"
    assert "msg-0-u" in note["content"], "提示里要列出真实 messages.id，否则取不回来"
    assert "read_tool_result" not in note["content"], "两者不该同时出现在一条提示里"


def test_recall_block_carries_ref_per_message():
    """召回块逐条带 ref：模型才能按需精确取回某一条，而不是整段重读。"""
    block = _serialize_for_recall(_msg("user", "绝不要动 vendor/", id="m-42"))

    assert "ref=m-42" in block, "缺 ref 的话召回块只是一段无出处的文本"


def test_recall_block_without_id_stays_unchanged():
    """没有 id 的老会话不能凭空多出 ref= 字样。"""
    block = _serialize_for_recall(_msg("user", "旧消息"))

    assert "ref=" not in block


def test_tool_message_rebuild_preserves_id():
    """重建 tool 消息时只挑三个字段，`id` 必须一起带过去。

    否则上层拿到的消息集里 tool 消息没有 id，裁剪通知就无法为它给出 ref ——
    这正是「工具结果被裁掉后彻底不可召回」的那条路径。
    """
    messages = [
        _msg("system", "sys"),
        _msg("user", "跑命令"),
        _msg("assistant", "", tool_calls=[{"id": "c1", "name": "shell_exec", "arguments": "{}"}]),
        _msg("tool", "z" * 40_000, tool_call_id="c1", id="msg-tool-1"),
        _msg("user", "继续"),
        _msg("assistant", "好"),
    ]
    dropped: list[tuple[str, str]] = []

    kept = _prune_messages(messages, CompactionConfig(max_messages=3), dropped=dropped)

    note = next((m for m in kept if m["role"] == "user" and "pruned" in m["content"]), None)
    assert note is not None, "应有裁剪提示"
    assert "read_archive" in note["content"], "有 messages.id 时应指向持久归档"
    assert "msg-tool-1" in note["content"], "被裁掉的 tool 消息 id 必须出现在提示里"


# ── 折叠索引：session_folds 的写入契约 ──
#
# 这些记录是"哪段 seq 被折叠过"的唯一证据。区间错一位就会把没被裁的消息
# 也标成已折叠，`read_archive` 的指引随之错乱；而 `digest` 写成占位串，
# 等于字段填了却没有信息量，事后排查反而被误导。


def _conversation_with_seq(n_pairs: int) -> list[dict[str, Any]]:
    """带 `id` + `seq` 的对话，模拟从 PG 读回来的真实形态（seq 从 1 起）。"""
    out: list[dict[str, Any]] = [_msg("system", "你是助手")]
    seq = 1
    for i in range(n_pairs):
        out.append(_msg("user", f"问题 {i}", id=f"m{seq}", seq=seq))
        seq += 1
        out.append(_msg("assistant", f"回答 {i}", id=f"m{seq}", seq=seq))
        seq += 1
    return out


def test_fold_records_the_pruned_range_and_kept_tail():
    """区间必须精确覆盖被裁的那段，`body_suffix` 必须等于保留条数。"""
    messages = _conversation_with_seq(5)  # 1 system + 10 条
    folds: list[dict[str, Any]] = []

    # max_messages=3 ⇒ budget = 3-1 = 2 ⇒ 保留最后 2 条（seq 9、10）
    _prune_messages(messages, CompactionConfig(max_messages=3), folds=folds)

    assert len(folds) == 1, "一次裁剪只应产生一条折叠记录"
    fold = folds[0]
    assert fold["covered_from"] == 1
    assert fold["covered_to"] == 9, "半开区间：覆盖 seq 1..8，上界是 9 而不是 8"
    assert fold["body_suffix"] == 2, "body_suffix 是投影时原样拼接的尾部条数"


def test_fold_digest_is_real_content_not_placeholder():
    """摘要必须是**原文线索**，不能是 `[N message(s) folded]` 这种占位串。

    占位串的危害是隐性的：字段看起来填过了，排查时却毫无信息量 ——
    让人以为折叠记录已经可用，从而放弃去查真正的原文。
    """
    messages = _conversation_with_seq(5)
    folds: list[dict[str, Any]] = []

    _prune_messages(messages, CompactionConfig(max_messages=3), folds=folds)

    digest = folds[0]["digest"]
    assert digest, "digest 不能为空（该列 NOT NULL，且是排查入口）"
    assert "message(s) folded" not in digest, "占位串不算摘要"
    # 被折叠区间里的原话必须能在摘要里找到
    assert "问题 0" in digest
    assert "回答 3" in digest


def test_fold_digest_marks_its_own_provenance():
    """摘要来源要如实标注：提取式 ≠ 模型生成，读方据此判断可信度。"""
    messages = _conversation_with_seq(5)
    folds: list[dict[str, Any]] = []

    _prune_messages(messages, CompactionConfig(max_messages=3), folds=folds)

    assert folds[0]["digest_model"] == "extractive"


def test_no_fold_when_seq_is_missing():
    """缺 seq 就不记区间 —— 宁可没有记录，也不能记一个会误覆盖的区间。

    Go 直传的消息、或 0016 之前写入的老消息都没有 seq。此时
    `covered_seqs` 不完整，若仍按 min/max 记录，区间会把中间那些
    本该保留的消息一并标成"已折叠"。
    """
    messages = [_msg("system", "sys"), _msg("user", "a"), _msg("assistant", "b")]
    folds: list[dict[str, Any]] = []

    _prune_messages(messages, CompactionConfig(max_messages=2), folds=folds)

    assert folds == []
