"""上下文投影（`_apply_folds`）的行为契约。

回归背景：`session_folds` 建表并写入后，`body_suffix` 一直没有读取方 ——
折叠记录只服务于排查，从未参与上下文组装。补上读回路径时，最关键的不是
"摘要能不能拼出来"，而是**什么时候必须拒绝拼接**：投影看起来完整、实则漏掉
一段上下文，比直接报错危险得多。

所以本文件的重点在拒绝路径：区间断裂、尾部不足、缺坐标、投影不更省。
"""

from __future__ import annotations

from typing import Any

from app.agent.compaction import _apply_folds


def _m(role: str, content: str, seq: int | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"role": role, "content": content}
    if seq is not None:
        out["seq"] = seq
    out.update(extra)
    return out


def _conversation(n: int) -> list[dict[str, Any]]:
    """system + n 条消息（seq 1..n）。"""
    out: list[dict[str, Any]] = [_m("system", "你是助手")]
    for i in range(1, n + 1):
        out.append(_m("user" if i % 2 else "assistant", f"内容 {i}", seq=i))
    return out


def _fold(frm: int, to: int, digest: str = "- [user] 早前要点", body_suffix: int = 0) -> dict[str, Any]:
    return {
        "covered_from": frm,
        "covered_to": to,
        "digest": digest,
        "digest_model": "extractive",
        "body_suffix": body_suffix,
    }


# ── 正常路径 ──


def test_projection_replaces_covered_range_with_digest():
    """被折叠区间只剩摘要，尾部原样保留，system 提示仍在最前。"""
    messages = _conversation(6)

    projected = _apply_folds(messages, [_fold(1, 4, body_suffix=3)])

    assert projected is not None
    assert projected[0]["role"] == "system", "冻结体（系统提示）必须留在最前"
    assert projected[1]["role"] == "user", "摘要用 user 角色，system 段会被重排吞掉"
    assert "早前要点" in projected[1]["content"]
    assert "read_archive" in projected[1]["content"], "摘要要指回可查入口"

    tail = projected[2:]
    assert [m["seq"] for m in tail] == [4, 5, 6], "尾部是从 covered_to 起原样保留的消息"


def test_projection_is_idempotent():
    """同一输入两次结果逐字段相等 —— 投影必须是纯函数。

    这是验收 #3「清空缓存后重建结果不变」的前提：有随机性或依赖外部状态的
    投影，缓存前后的结果会对不上。
    """
    messages = _conversation(6)
    folds = [_fold(1, 4, body_suffix=3)]

    first = _apply_folds(messages, folds)
    second = _apply_folds(messages, folds)

    assert first == second


def test_multiple_contiguous_folds_are_all_summarized():
    """相邻两段折叠首尾相接时，两段摘要都进入投影。"""
    messages = _conversation(8)
    folds = [_fold(1, 4, digest="- [user] 第一段", body_suffix=0), _fold(4, 7, digest="- [user] 第二段", body_suffix=2)]

    projected = _apply_folds(messages, folds)

    assert projected is not None
    content = projected[1]["content"]
    assert "第一段" in content
    assert "第二段" in content
    assert [m["seq"] for m in projected[2:]] == [7, 8]


# ── 拒绝路径（最重要） ──


def test_gap_between_folds_fails_open():
    """两段折叠之间有断档 ⇒ 那段消息既没进摘要也没进尾部，必须拒绝。

    宁可回退全量（超预算就走实时裁剪），也不发一份漏了上下文的投影。
    """
    messages = _conversation(10)
    folds = [_fold(1, 4), _fold(5, 7)]  # 4 ≠ 5，断档

    assert _apply_folds(messages, folds) is None


def test_tail_shorter_than_recorded_body_suffix_fails_open():
    """尾部条数少于记录时的 `body_suffix` ⇒ 数据比记录时还少，投影不可信。"""
    messages = _conversation(6)
    # body_suffix=5 表示当时留了 5 条尾部，但 covered_to=6 之后只剩 1 条
    folds = [_fold(1, 6, body_suffix=5)]

    assert _apply_folds(messages, folds) is None


def test_missing_seq_fails_open():
    """有消息缺 seq 就没有坐标，算不出折叠边界 —— 拒绝而不是猜。"""
    messages = [
        _m("system", "sys"),
        _m("user", "有坐标", seq=1),
        _m("assistant", "没坐标"),  # 缺 seq
        _m("user", "有坐标", seq=3),
    ]

    assert _apply_folds(messages, [_fold(1, 2, body_suffix=2)]) is None


def test_empty_digest_fails_open():
    """摘要全为空时投影只剩尾部 —— 那等于把折叠掉的内容彻底删掉。"""
    messages = _conversation(6)
    folds = [_fold(1, 4, digest="", body_suffix=3)]

    assert _apply_folds(messages, folds) is None


def test_no_folds_returns_none():
    """没有折叠记录就不该走投影（调用方据此回退全量）。"""
    assert _apply_folds(_conversation(6), []) is None


def test_message_outside_any_fold_fails_open():
    """尾部之前存在不属于任何 fold 的消息 ⇒ 它既没进摘要也没留尾部，必须拒绝。

    这条校验此前写成了恒等式（`len(ordered) - len(tail)`），等于没检查；
    换成「逐条判断是否被某个 fold 覆盖」后才有意义。
    """
    messages = _conversation(8)
    # fold 只覆盖 1..3 和 5..7，seq=4 落在缝隙里
    folds = [_fold(1, 3, digest="- [user] 段一", body_suffix=0), _fold(5, 7, digest="- [user] 段二")]

    assert _apply_folds(messages, folds) is None


def test_projection_that_is_not_smaller_returns_none():
    """摘要比原文还长时宁可用全量 —— 投影的意义是省预算。"""
    messages = _conversation(4)
    # 正文极短，摘要极长：投影反而更大
    messages = [_m("system", "s")] + [_m("user", "x", seq=i) for i in range(1, 5)]
    folds = [_fold(1, 4, digest="y" * 5000, body_suffix=1)]

    # model 为空时跳过预算比较，因此这里必须给出 model 才会触发该分支；
    # 用不存在的模型名时 estimator 会回落默认系数，仍能比较大小。
    assert _apply_folds(messages, folds, model="gpt-4o") is None
