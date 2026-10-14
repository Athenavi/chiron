"""D2：推理参数契约表（app/providers/reasoning_contract.py）单测。

钉住两条：**值按后端映射**、**放不对位置就不发**（宁可退化也不发对方必然拒绝的字段）。
"""

from __future__ import annotations

from app.providers.reasoning_contract import (
    STYLE_NESTED,
    STYLE_NONE,
    STYLE_TOP_LEVEL,
    describe,
    resolve,
    resolve_contract,
)


def test_openai_uses_top_level_field() -> None:
    assert resolve("high", provider="openai", model="gpt-5") == {"reasoning_effort": "high"}


def test_responses_uses_nested_field() -> None:
    assert resolve("low", provider="openai-responses") == {"reasoning": {"effort": "low"}}


def test_anthropic_maps_to_thinking_flag() -> None:
    assert resolve("medium", provider="anthropic") == {"thinking": {"type": "enabled"}}


def test_deepseek_sends_nothing() -> None:
    # DeepSeek 直连没有档位参数 —— 发任何字段都可能被拒。
    assert resolve("high", provider="deepseek", model="deepseek-chat") == {}


def test_unknown_provider_sends_nothing() -> None:
    assert resolve("high", provider="who-knows") == {}
    assert resolve("high", model="mystery-model") == {}


def test_off_effort_sends_nothing() -> None:
    # 关思考的写法归一化后为空 ⇒ 不发字段（显式发 none 同样可能 400）。
    assert resolve("disabled", provider="openai") == {}
    assert resolve("", provider="openai") == {}
    assert resolve(None, provider="openai") == {}


def test_provider_wins_over_model_name() -> None:
    # 同名模型经不同网关接入时，决定参数形态的是**协议**（provider），不是模型名。
    assert resolve("high", provider="deepseek", model="gpt-5") == {}


def test_longest_prefix_wins() -> None:
    # "openai-responses" 比 "openai" 更长 ⇒ 命中 nested 契约。
    c = resolve_contract("openai-responses", "")
    assert c is not None and c.style == STYLE_NESTED
    c2 = resolve_contract("openai", "")
    assert c2 is not None and c2.style == STYLE_TOP_LEVEL


def test_max_degrades_to_high_for_openai() -> None:
    # SDK 词表没有 max（effort.py 的归一化把 max → high）。
    assert resolve("max", provider="openai") == {"reasoning_effort": "high"}


def test_no_key_at_all_returns_none_contract() -> None:
    assert resolve_contract("", "") is None


def test_describe_shape() -> None:
    rows = describe()
    assert len(rows) >= 4
    for r in rows:
        assert "prefixes" in r and "style" in r and "notes" in r
    # style=none 的行不展示 field（它没有字段）。
    for r in rows:
        if r["style"] == STYLE_NONE:
            assert r["field"] == ""
