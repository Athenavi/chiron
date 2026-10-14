"""output_controls — 用户可见的**输出控制**：输出风格 + 语言偏好（差距B）。

对照实现：Reasonix 的 `internal/runtime/{outputstyle,langpref}`（输出风格与语言偏好）。
Chiron 此前只有两层塑形，缺的正是这一层**用户可见、可在界面上切换**的控制：

* `modes.py`：按**运行模式**给 persona（这次要干什么）；
* `harness_profile.py`：按**模型家族**给 suffix（这个模型该怎么表现）。

两者都不是"用户想要什么风格 / 什么语言"。本模块补上这一层，且遵守与 harness 相同的两条纪律：

1. **默认值逐字不变**：`style=default` 且 `language=auto` 时 `build_controls_suffix` 返回**空串**，
   于是 system 段与引入本模块之前**逐字相同**（不改变任何现有会话的输出）。
2. **落在稳定前缀的末尾**：片段由 runtime 追加在 harness suffix 之后、记忆之前 —— 同会话同选择下
   逐字稳定 ⇒ 前缀缓存仍然命中（与 `harness_store` 的 `extra_suffix` 同一处理）。

本模块是**纯逻辑**（无 IO、无并发），因此可单测。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

#: 默认语言偏好：跟随用户提问的语言（不注入任何语言指令 ⇒ 零行为变化）。
LANGUAGE_AUTO = "auto"

#: 支持的语言偏好。与前端 i18n 的语言集保持一致（见 frontend-vue/src/locales）。
SUPPORTED_LANGUAGES: tuple[str, ...] = (LANGUAGE_AUTO, "zh-CN", "en-US", "ar")

#: 语言代码 → 给模型看的英文名（模型对英文名更可靠）。
_LANGUAGE_NAMES: dict[str, str] = {
    "zh-CN": "Simplified Chinese",
    "en-US": "English",
    "ar": "Arabic",
}


class OutputStyle(StrEnum):
    """输出风格。`DEFAULT` 表示"平台默认"，不注入任何风格指令。"""

    DEFAULT = "default"
    CONCISE = "concise"
    DETAILED = "detailed"
    TEACHING = "teaching"
    CODE = "code"


#: 风格 → system 指令。**只写"风格"本身**，不重复 persona / 安全要求（那些在别处）。
_STYLE_INSTRUCTIONS: dict[str, str] = {
    OutputStyle.CONCISE: (
        "Answer in the fewest words that fully answer the question. Lead with the "
        "conclusion. Skip preamble, restatement, and closing pleasantries. If the user "
        "needs a long explanation, prefer a short answer plus an offer to expand."
    ),
    OutputStyle.DETAILED: (
        "Answer thoroughly. State the conclusion first, then give the reasoning, the "
        "evidence you relied on, and any caveats or edge cases the user should know."
    ),
    OutputStyle.TEACHING: (
        "Teach rather than just answer: explain the underlying concept, why the approach "
        "works, and what a reader would get wrong. Use small concrete examples. Check "
        "understanding by naming the key idea explicitly."
    ),
    OutputStyle.CODE: (
        "Prefer showing working code over prose. Keep explanation to the minimum needed "
        "to use it (what it does, how to run it, what it assumes). Put code first, notes "
        "after."
    ),
}


def normalize_style(raw: Any) -> str:
    """归一化输出风格；未知取值回落 ``default``（fail-safe，不猜）。"""
    value = str(raw or "").strip().lower()
    if value in {s.value for s in OutputStyle}:
        return value
    return OutputStyle.DEFAULT.value


def normalize_language(raw: Any) -> str:
    """归一化语言偏好；未知取值回落 ``auto``。"""
    value = str(raw or "").strip()
    if value in SUPPORTED_LANGUAGES:
        return value
    # 容忍大小写/下划线差异（前端可能传 zh_CN / ZH-cn）
    lowered = value.lower().replace("_", "-")
    for lang in SUPPORTED_LANGUAGES:
        if lang.lower() == lowered:
            return lang
    return LANGUAGE_AUTO


def style_instruction(style: str) -> str:
    """风格对应的 system 片段；``default`` 返回空串。"""
    return _STYLE_INSTRUCTIONS.get(normalize_style(style), "")


def language_instruction(language: str) -> str:
    """语言偏好对应的 system 片段；``auto`` 返回空串。"""
    lang = normalize_language(language)
    if lang == LANGUAGE_AUTO:
        return ""
    name = _LANGUAGE_NAMES.get(lang, lang)
    return (
        f"Always reply in {name} ({lang}), regardless of the language of the question, "
        f"unless the user explicitly asks for another language."
    )


def build_controls_suffix(
    style: str = "", language: str = "", reasoning_language: str = ""
) -> str:
    """把风格、回复语言与**思考语言**拼成一段 suffix。

    **默认态（default + auto + auto）返回空串** —— 这是本模块最重要的性质：引入它不该改变
    任何现有会话的 system prompt（从而不改变输出、不影响前缀缓存）。

    思考语言（D2）与回复语言刻意分开：有人要"用英文思考、用中文回答"，
    也有人要"思考和回答同语言" —— 合成一个旋钮就表达不出来。
    """
    from app.providers.reasoning_language import reasoning_language_instruction

    parts = [
        p
        for p in (
            style_instruction(style),
            language_instruction(language),
            reasoning_language_instruction(reasoning_language),
        )
        if p
    ]
    if not parts:
        return ""
    return "## Output preferences\n" + "\n\n".join(parts)


def resolve_controls(llm_config: dict[str, Any] | None) -> tuple[str, str, str]:
    """从请求的 ``llm_config`` 解析 (style, language, reasoning_language)。

    取值来自前端（用户实时选择，随每次提交上传）—— 与 `tools_mode` 同一处置：**服务端不存储**，
    避免"前端选、Redis、runtime"三份状态漂移（这是本项目反复踩过的坑）。
    """
    from app.providers.reasoning_language import normalize_reasoning_language

    cfg = llm_config or {}
    return (
        normalize_style(cfg.get("output_style")),
        normalize_language(cfg.get("language")),
        normalize_reasoning_language(cfg.get("reasoning_language")),
    )


def describe_controls() -> dict[str, Any]:
    """给前端/管理端用的选项清单（单一真相，避免前端硬编码枚举）。"""
    return {
        "styles": [
            {"value": OutputStyle.DEFAULT.value, "i18n": "chat.style_default"},
            {"value": OutputStyle.CONCISE.value, "i18n": "chat.style_concise"},
            {"value": OutputStyle.DETAILED.value, "i18n": "chat.style_detailed"},
            {"value": OutputStyle.TEACHING.value, "i18n": "chat.style_teaching"},
            {"value": OutputStyle.CODE.value, "i18n": "chat.style_code"},
        ],
        "languages": [
            {"value": LANGUAGE_AUTO, "i18n": "chat.lang_auto"},
            {"value": "zh-CN", "i18n": "chat.lang_zh"},
            {"value": "en-US", "i18n": "chat.lang_en"},
            {"value": "ar", "i18n": "chat.lang_ar"},
        ],
    }
