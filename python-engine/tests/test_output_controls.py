"""差距B：输出控制（风格 / 语言偏好）与提示精炼单测。

钉住最关键的性质：**默认态零行为变化**（default + auto ⇒ 空 suffix），以及精炼失败
绝不丢用户输入。
"""

from __future__ import annotations

from typing import Any

from app.agent.output_controls import (
    LANGUAGE_AUTO,
    OutputStyle,
    build_controls_suffix,
    describe_controls,
    language_instruction,
    normalize_language,
    normalize_style,
    resolve_controls,
    style_instruction,
)
from app.agent.prompt_refine import refine_prompt

# ── 输出控制 ──


def test_default_state_produces_empty_suffix() -> None:
    # 这是本模块最重要的性质：默认态不改变 system 段（从而不改变输出、不影响前缀缓存）。
    assert build_controls_suffix("default", "auto") == ""
    assert build_controls_suffix("", "") == ""


def test_style_instruction_only_for_non_default() -> None:
    assert style_instruction("default") == ""
    assert "fewest words" in style_instruction("concise")
    assert style_instruction("code")


def test_language_instruction_only_for_non_auto() -> None:
    assert language_instruction("auto") == ""
    assert "Simplified Chinese" in language_instruction("zh-CN")
    assert "Arabic" in language_instruction("ar")


def test_suffix_combines_both() -> None:
    text = build_controls_suffix("concise", "zh-CN")
    assert "Output preferences" in text
    assert "fewest words" in text
    assert "Simplified Chinese" in text


def test_normalize_rejects_unknown() -> None:
    assert normalize_style("banana") == "default"
    assert normalize_style(None) == "default"
    assert normalize_language("klingon") == LANGUAGE_AUTO
    # 大小写 / 下划线容忍
    assert normalize_language("ZH_cn") == "zh-CN"


def test_resolve_controls_from_llm_config() -> None:
    # D2 起返回三元组：风格 / 回复语言 / **思考语言**（后者可独立设置）。
    assert resolve_controls({"output_style": "detailed", "language": "en-US"}) == (
        "detailed", "en-US", LANGUAGE_AUTO,
    )
    assert resolve_controls({"reasoning_language": "zh-CN"}) == ("default", LANGUAGE_AUTO, "zh-CN")
    assert resolve_controls(None) == ("default", LANGUAGE_AUTO, LANGUAGE_AUTO)
    assert resolve_controls({}) == ("default", LANGUAGE_AUTO, LANGUAGE_AUTO)


def test_controls_suffix_includes_reasoning_language() -> None:
    text = build_controls_suffix("", "", "zh-CN")
    assert "Think and reason in Simplified Chinese" in text
    # 默认态（三个 auto/default）必须仍然是空串。
    assert build_controls_suffix("default", "auto", "auto") == ""


def test_all_styles_have_instructions_except_default() -> None:
    for style in OutputStyle:
        if style is OutputStyle.DEFAULT:
            assert style_instruction(style.value) == ""
        else:
            assert style_instruction(style.value), f"{style} must have an instruction"


def test_describe_controls_shape() -> None:
    d = describe_controls()
    assert {s["value"] for s in d["styles"]} == {s.value for s in OutputStyle}
    assert {lang["value"] for lang in d["languages"]} == {"auto", "zh-CN", "en-US", "ar"}


# ── 提示精炼 ──


class _Gw:
    def __init__(self, content: str = "refined prompt", boom: bool = False) -> None:
        self._c = content
        self._boom = boom
        self.calls = 0

    async def chat(self, messages: Any, model: str, max_tokens: int, temperature: float = 0.0) -> Any:  # noqa: ANN401
        self.calls += 1
        if self._boom:
            raise RuntimeError("provider down")

        class _R:
            content = self._c
            input_tokens = 1
            output_tokens = 2

        return _R()


async def test_refine_success() -> None:
    r = await refine_prompt(_Gw("Write a function that ..."), raw="写个函数", model="m")
    assert r.ok is True
    assert r.refined == "Write a function that ..."


async def test_refine_empty_input_is_rejected_without_calling_model() -> None:
    gw = _Gw()
    r = await refine_prompt(gw, raw="   ", model="m")
    assert r.ok is False
    assert gw.calls == 0  # 空输入不该白花一次调用


async def test_refine_failure_does_not_lose_input() -> None:
    # 失败时 ok=False 且 refined 为空 —— 调用方据此保留用户原文。
    r = await refine_prompt(_Gw(boom=True), raw="原始输入", model="m")
    assert r.ok is False
    assert r.refined == ""
    assert r.error


async def test_refine_empty_output_is_failure() -> None:
    r = await refine_prompt(_Gw("   "), raw="x", model="m")
    assert r.ok is False
