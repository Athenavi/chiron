"""工具结果的分级压缩（app/agent/result_compactor.py）。

回归保护：**截断 ≠ 摘要**。`head + tail` 会把日志里的错误行、长列表里的关键项、JSON 的中间
结构整段挖掉 —— 模型拿到被挖空的文本，更容易误判或重复调用同一个工具。这组断言钉住
"按形态摘要"的三个关键行为。
"""
import json

from app.agent.result_compactor import (
    COMPACT,
    INLINE,
    INLINE_LIMIT,
    REF_ONLY,
    classify,
    summarize,
)


def test_classify_tiers_by_size():
    assert classify("small") == INLINE
    assert classify("x" * (INLINE_LIMIT + 1)) == COMPACT
    assert classify("x" * (256 * 1024 + 1)) == REF_ONLY


def test_short_text_passes_through_unchanged():
    text = "hello world"
    assert summarize(text) == text


def test_json_summary_gives_structure_not_slices():
    payload = {"items": [{"id": i} for i in range(500)], "note": "x" * 5000}
    out = summarize(json.dumps(payload))
    # 结构信息（键名 + 数组长度）必须出现
    assert "items" in out
    assert "500" in out
    assert "note" in out
    # 不是"把头尾拼起来"：不会把 5000 个 x 的尾巴搬进来
    assert "x" * 200 not in out


def test_line_summary_surfaces_middle_error_lines():
    lines = [f"line {index}" for index in range(400)]
    lines[200] = "ERROR: disk full at /data"
    out = summarize("\n".join(lines))
    # 中段的关键行被单独挑出来（head+tail 会丢掉它）
    assert "disk full" in out
    assert "400" in out  # 总行数


def test_line_summary_says_so_when_no_errors():
    lines = [f"line {index}" for index in range(100)]
    out = summarize("\n".join(lines))
    assert "无 error" in out


def test_path_list_is_aggregated_by_directory():
    paths = [f"src/mod{index % 5}/file{index}.py" for index in range(60)]
    out = summarize("\n".join(paths))
    assert "60" in out
    assert "src/mod0" in out


def test_fallback_is_head_tail():
    # 短行（不足以走行文本路径）且不是 JSON → 兜底 head+tail，且标明省略了多少
    text = "一二三四五六七八九十" * 200
    out = summarize(text, budget_chars=100)
    assert "省略" in out
    assert len(out) < len(text)
