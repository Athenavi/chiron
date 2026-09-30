"""A2（方案 04 批次 1）：checkpoint 快照的 schema 版本。

**为什么需要它**：没有版本时，"旧引擎读到新格式快照"是**静默**的 —— 旧代码按老字段名取值，
取不到就落到默认值，现场被悄悄读歪。有了版本，读取侧可以**明确拒绝**（对齐 Reasonix 的
turn ledger：未知版本 `leave it untouched`）。

三条不变量：

1. 新写的快照带版本号；
2. **缺字段**的快照（本字段引入前的形态）按 v1 兼容读取 —— 显式承认，不是猜测；
3. 版本**高于**本引擎支持时拒绝（返回 None + 日志），且**不做任何写操作**。
"""

from __future__ import annotations

import json

from app.agent import checkpoint as ckpt
from app.agent.resume import _parse_snapshot


def test_new_snapshot_carries_schema_version():
    snap = ckpt.build_snapshot(messages=[{"role": "user", "content": "hi"}], turn_index=1)

    assert snap["schema_version"] == ckpt.SNAPSHOT_SCHEMA_VERSION


def test_snapshot_without_version_is_read_as_v1():
    """本字段引入前写下的快照必须仍可续跑 —— 否则升级就是一次"现场全丢"。"""
    legacy = json.dumps({"turn_index": 3, "messages": [{"role": "user", "content": "x"}]})

    parsed = _parse_snapshot(legacy)

    assert parsed is not None
    assert parsed["turn_index"] == 3


def test_current_version_snapshot_is_accepted():
    payload = {"schema_version": ckpt.SNAPSHOT_SCHEMA_VERSION, "turn_index": 2}

    assert _parse_snapshot(json.dumps(payload)) == payload


def test_unknown_future_version_is_rejected(caplog):
    """更高的版本 = 更新的引擎写的。按老字段名取值会静默读歪 ⇒ 宁可放弃续跑。"""
    future = json.dumps({"schema_version": ckpt.SNAPSHOT_SCHEMA_VERSION + 1, "turn_index": 9})

    with caplog.at_level("WARNING"):
        assert _parse_snapshot(future) is None

    assert any("拒绝未知快照 schema 版本" in r.message for r in caplog.records)


def test_non_integer_version_is_treated_as_unknown():
    """非法值（字符串等）不能当成"老版本"放过去。"""
    assert _parse_snapshot(json.dumps({"schema_version": "2", "turn_index": 1})) is None


def test_malformed_payload_still_returns_none():
    """既有行为不变：不是 JSON / 不是 dict 时返回 None。"""
    assert _parse_snapshot("{not json") is None
    assert _parse_snapshot("[1,2]") is None
    assert _parse_snapshot("") is None
