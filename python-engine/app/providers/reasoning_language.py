"""思考语言控制（D2）—— 让"可见的思考过程"用用户读得懂的语言。

## 为什么需要它

有些模型会把推理过程**显式输出**（native reasoning / `thinking` 通道，Chiron 经独立
`thinking` 事件下发）。默认情况下这段思考的语言往往随提示词与训练偏好漂移 ——
中文用户的思考过程可能是英文（或混着来），读起来既慢又别扭。

这与 `output_controls` 的**回复语言**是两件事：

* 回复语言（`output_controls.language`）：**答案**用什么语言 —— 那是交付物；
* 思考语言（本模块）：**推理过程**用什么语言 —— 那是过程。

两者要能分开设：有人要"用英文思考、用中文回答"（便于他复用英文思考链），
也有人要"思考和回答都用中文"。合在一起就没法表达。

## 实现方式

不能靠 provider 参数（各家没有"思考语言"这个旋钮），所以走**提示词**：在 system 段追加一句
"用 X 语言推理"。因此它必须落在**稳定前缀**里（同会话同选择下逐字不变），否则每轮变化会
把前缀缓存打掉 —— 与 `output_controls` / harness suffix 同一处置。

**默认（空 / auto）返回空串**：不注入任何指令，逐字不变。
"""

from __future__ import annotations

from typing import Any

#: 与 output_controls 的语言集保持一致（前端共用一份选择器语义）。
LANGUAGE_AUTO = "auto"

SUPPORTED_REASONING_LANGUAGES: tuple[str, ...] = (LANGUAGE_AUTO, "zh-CN", "en-US", "ar")

_LANGUAGE_NAMES: dict[str, str] = {
    "zh-CN": "Simplified Chinese",
    "en-US": "English",
    "ar": "Arabic",
}


def normalize_reasoning_language(raw: Any) -> str:
    """归一化思考语言偏好；未知取值回落 ``auto``（不猜）。"""
    value = str(raw or "").strip()
    if value in SUPPORTED_REASONING_LANGUAGES:
        return value
    lowered = value.lower().replace("_", "-")
    for lang in SUPPORTED_REASONING_LANGUAGES:
        if lang.lower() == lowered:
            return lang
    return LANGUAGE_AUTO


def reasoning_language_instruction(language: str) -> str:
    """生成 system 片段；``auto`` 返回**空串**（不注入 ⇒ 逐字不变）。"""
    lang = normalize_reasoning_language(language)
    if lang == LANGUAGE_AUTO:
        return ""
    name = _LANGUAGE_NAMES.get(lang, lang)
    return (
        f"Think and reason in {name} ({lang}). Your visible reasoning should be in that "
        f"language; only the final answer follows the reply-language setting."
    )


def describe_reasoning_languages() -> list[dict[str, str]]:
    """给前端的选择项（单一真相，避免前端硬编码）。"""
    return [
        {"value": LANGUAGE_AUTO, "i18n": "chat.think_lang_auto"},
        {"value": "zh-CN", "i18n": "chat.think_lang_zh"},
        {"value": "en-US", "i18n": "chat.think_lang_en"},
        {"value": "ar", "i18n": "chat.think_lang_ar"},
    ]
