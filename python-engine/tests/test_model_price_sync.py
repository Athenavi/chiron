"""M28：按模型的公开价格 —— 解析、归一化、交叉验证。

三条不变量：

1. **单位统一到 USD / 1000 tokens**：各源单位不同（portkey = 美分/token；llmrates = 每百万
   token + 原生货币），换算只在各自的解析函数里做一次；
2. **至少两个源一致（相对差 ≤ 5%）才收**：单源不写入、分歧不写入 —— 宁可缺数据，也不要
   一个来源不明或自相矛盾的数字；
3. **非 USD 跳过**（本仓不做汇率换算，价格表也不参与计费，没有必须换算的压力）。

payload 片段取自两个源的真实响应（2026-10-22 实测）。
"""
from __future__ import annotations

import pytest

from app.pricing import sync as s

# ── 解析：真实 payload 的形状 ──


def test_portkey_cents_per_token_becomes_usd_per_1k() -> None:
    """portkey 的单位是**美分 / token**（其文档自陈）⇒ USD/1k = 美分 × 10。"""
    payload = {
        "gpt-4o": {
            "pricing_config": {
                "pay_as_you_go": {
                    "request_token": {"price": 0.00025},   # 美分/token ⇒ $2.5/1M
                    "response_token": {"price": 0.001},
                }
            }
        }
    }

    prices = s.parse_portkey(payload)

    assert prices["gpt-4o"].input_per_1k == pytest.approx(0.0025)
    assert prices["gpt-4o"].output_per_1k == pytest.approx(0.01)


def test_llmrates_per_million_becomes_usd_per_1k() -> None:
    payload = {
        "models": [
            {
                "name": "gpt-4o",
                "provider": {"slug": "openai"},
                "prices": [{"inputPricePerMillion": 2.5, "outputPricePerMillion": 10, "priceUnit": "USD"}],
            }
        ]
    }

    prices = s.parse_llmrates(payload)

    assert prices["gpt-4o"].input_per_1k == pytest.approx(0.0025)
    assert prices["gpt-4o"].output_per_1k == pytest.approx(0.01)


def test_llmrates_skips_non_usd() -> None:
    """非 USD 一律跳过（不做汇率换算）—— 否则价格表里会混进"看起来是美元"的人民币。"""
    payload = {
        "models": [
            {
                "name": "glm-4v",
                "provider": {"slug": "zhipu"},
                "prices": [{"inputPricePerMillion": 10, "outputPricePerMillion": 10, "priceUnit": "CNY"}],
            }
        ]
    }

    assert s.parse_llmrates(payload) == {}


def test_model_name_normalization_drops_date_suffix() -> None:
    assert s.normalize_model("GPT-4o-2024-11-20") == "gpt-4o"
    assert s.normalize_model("claude_3.5_sonnet") == "claude-3-5-sonnet"


# ── 交叉验证 ──


def _price(model: str, inp: float, outp: float) -> s.ModelPrice:
    return s.ModelPrice(model=model, provider="openai", input_per_1k=inp, output_per_1k=outp)


def test_two_sources_within_tolerance_agree() -> None:
    agreed, disagreements = s.cross_validate(
        {"a": {"gpt-4o": _price("gpt-4o", 0.0025, 0.01)}, "b": {"gpt-4o": _price("gpt-4o", 0.0026, 0.0101)}}
    )

    assert [p.model for p in agreed] == ["gpt-4o"]
    assert disagreements == []


def test_disagreement_is_not_written_but_is_reported() -> None:
    """分歧**不写入**，但**不静默** —— 否则"价格表一直是空的"没人知道为什么。"""
    agreed, disagreements = s.cross_validate(
        {"a": {"gpt-4o": _price("gpt-4o", 0.0025, 0.01)}, "b": {"gpt-4o": _price("gpt-4o", 0.05, 0.01)}}
    )

    assert agreed == []
    assert len(disagreements) == 1 and "gpt-4o" in disagreements[0]


def test_output_axis_alone_is_enough_to_disagree() -> None:
    """两轴都要比：只比输入会让"输出价差一倍"溜过去。"""
    agreed, _ = s.cross_validate(
        {"a": {"m": _price("m", 0.0025, 0.01)}, "b": {"m": _price("m", 0.0025, 0.5)}}
    )

    assert agreed == []


def test_single_source_is_never_written() -> None:
    """只有一个源 ⇒ 判未验证、不写入（单源无法交叉验证）。"""
    agreed, disagreements = s.cross_validate({"a": {"gpt-4o": _price("gpt-4o", 0.0025, 0.01)}})

    assert agreed == []
    assert disagreements == []


def test_model_names_that_do_not_align_become_single_source() -> None:
    """两个源对同一个模型的叫法差得远 ⇒ 对齐不上 ⇒ 各自单源 ⇒ 都不写入。

    这是**有意的保守**：拿两个不同模型的价格互相"验证"比没有数据更糟。
    """
    agreed, _ = s.cross_validate(
        {"a": {"glm-4v": _price("glm-4v", 0.01, 0.01)}, "b": {"glm-4-vision": _price("glm-4-vision", 0.01, 0.01)}}
    )

    assert agreed == []


# ── 开关 ──


def test_sync_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICE_SYNC", raising=False)

    assert s.sync_enabled() is False


def test_interval_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PRICE_SYNC_INTERVAL_SECONDS", "abc")
    assert s.sync_interval_seconds() == s.DEFAULT_INTERVAL_SECONDS

    monkeypatch.setenv("MODEL_PRICE_SYNC_INTERVAL_SECONDS", "5")  # 太小 ⇒ 回落
    assert s.sync_interval_seconds() == s.DEFAULT_INTERVAL_SECONDS


async def test_start_price_sync_returns_immediately_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认关 ⇒ 调用方可以无条件 await 它，不必自己判开关。"""
    monkeypatch.delenv("MODEL_PRICE_SYNC", raising=False)

    await s.start_price_sync()  # 不会挂起


async def test_sync_once_writes_nothing_when_no_source_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """所有源都拉不到 ⇒ 一行都不写，且**不抛**（一轮失败不该让循环死掉）。"""

    async def _dead(_name: str, _url: str, **_kw: object) -> None:
        return None

    monkeypatch.setattr(s, "_fetch_source", _dead)
    written, disagreements = await s.sync_once()

    assert written == 0 and disagreements == []
