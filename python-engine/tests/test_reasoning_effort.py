"""思考档位归一化：**只发对方认的值**。

回归背景（真实故障）：`effort=medium` 发给 `deepseek-flash` 得到
`UNSUPPORTED_REASONING_EFFORT`。根因是各 provider 对档位的词表不一致 ——
"关闭思考"有十来种写法，而 SDK 只认 `minimal/low/medium/high`（没有 `max`、
也没有统一的"关"）。
"""

from app.providers.effort import normalize_reasoning_effort


def test_off_variants_become_empty():
    """表示"关闭"的所有写法都必须归一成空串（= 不发送该字段）。"""
    for raw in (
        "disabled",
        "false",
        "no",
        "none",
        "nothink",
        "no-think",
        "no_think",
        "off",
        "0",
        "OFF",
        "  Disabled  ",
    ):
        assert normalize_reasoning_effort(raw) == "", raw


def test_our_levels_map_to_sdk_levels():
    assert normalize_reasoning_effort("minimal") == "minimal"
    assert normalize_reasoning_effort("low") == "low"
    assert normalize_reasoning_effort("high") == "high"
    # SDK 没有 max → 降到 high（宁降档，也不要发一个必然被拒的值）
    assert normalize_reasoning_effort("max") == "high"


def test_on_variants_map_to_medium():
    for raw in ("on", "true", "enabled", "enable", "auto", "default", "yes", "1"):
        assert normalize_reasoning_effort(raw) == "medium", raw


def test_unknown_and_non_string_are_dropped():
    """无法识别一律"不发送"，绝不透传未知值。"""
    for raw in ("ultra", "medium-plus", "", "   ", None, 123, [], {}, True):
        assert normalize_reasoning_effort(raw) == "", repr(raw)
