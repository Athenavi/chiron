"""模型能力（模态）—— 以**接口的实际反馈**为准，而不是静态预设。

对齐 Reasonix 的两条判据（见 `vendor/DeepSeek-Reasonix/REASONIX.md` 的 *Curated presets*）：
**声明优先于猜测**，模型名只是最后手段 —— 因为同一个名字在不同网关下能力可以不同
（自建 one-api 转发的 `deepseek` 与官方端点不必一致）。

三种来源，按可信度排序：

1. **运行时纠正**（``record_vision_refusal``）：provider 真的以"不支持图片"拒绝过 ⇒ 该模型
   不读图。这是"从当前选用模型的实际调用结果里学到的"，最可信，也正是"实时获取"的主路径；
2. **显式注入**（``remember_supports_vision``）：管理端/测试已知答案时直接写入；
3. **默认乐观**（``True``）：没有任何来源时按既有行为放行，并由第 1 条在一个回合内纠正。

模型名**不作判据**：一个名字在不同网关下的能力可以不同，按名字猜正是要避免的事。

未命中任何来源时**默认乐观**（``True``）：图片原本就是直发的（既有行为），把它改成
默认不发会让本来能读图的模型反而看不到图；判错的代价由第 1 条在一个回合内收敛。

为什么要这一层：把图片塞给读不了图的模型，provider 会整轮报错；而"读得懂就发、
读不懂就提示"必须是**一处**判断 —— 各调用点自己 try 只会得到不一致的行为。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

#: 能力缓存的 TTL（秒）。与 `model_window.WINDOW_CACHE_TTL_SECONDS` 同一取向：
#: 能力属"低频变更、高频读取"，TTL 内最多滞后这么久。
CAPABILITY_TTL_SECONDS = 300

#: 进程内缓存：model → (supports_vision, 记录时刻)
_cache: dict[str, tuple[bool, float]] = {}

#: 模型名声明表（**最后手段**，子串匹配）。只声明"读图"的形态，不写否认条目 ——
#: 未声明交给默认乐观，避免把能用图的模型误判成不能。
_VISION_HINTS: tuple[str, ...] = (
    "-vision",
    "vision-",
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "claude-3",
    "claude-4",
    "gemini",
    "glm-4v",
    "glm-5v",
    "qwen-vl",
    "qwen2-vl",
    "qwen3-vl",
    "llava",
    "internvl",
    "pixtral",
    "minicpm-v",
)




def cached_supports_vision(model: str) -> bool | None:
    """同步读缓存；未命中/已过期返回 ``None``（不阻塞、不查库）。"""
    hit = _cache.get(model)
    if hit is None or time.time() - hit[1] >= CAPABILITY_TTL_SECONDS:
        return None
    return hit[0]


def remember_supports_vision(model: str, supported: bool) -> None:
    """写入缓存（供测试与"已知能力"的场景直接注入）。"""
    if model:
        _cache[model] = (supported, time.time())


def record_vision_refusal(model: str) -> None:
    """provider 以"不支持图片"拒绝时调用 —— 之前的判断错了，就地纠正。

    这是本模块的**主路径**：不靠预设，而靠"当前选用的模型"自己的回答。
    """
    if not model:
        return
    if _cache.get(model, (True, 0.0))[0]:
        logger.info(
            "model %s refused an image input; treating it as vision-less for %ds",
            model,
            CAPABILITY_TTL_SECONDS,
        )
    remember_supports_vision(model, False)


def clear_cache() -> None:
    """清空缓存（测试用）。"""
    _cache.clear()


async def supports_vision(model: str) -> bool:
    """该模型能否收图：缓存 → 默认乐观（``True``）。

    **名字不是判据** —— 判据来自接口的实际反馈（``record_vision_refusal``）。保持
    ``async`` 是为了与 `model_window.context_window` 对称，并给"主动探测 provider
    端点"留位置（官方 OpenAI / Anthropic 的 ``/models`` 不返回模态，所以主动探测不是
    主路径）。
    """
    if not model:
        return False
    hit = cached_supports_vision(model)
    if hit is not None:
        return hit
    remember_supports_vision(model, True)
    return True
