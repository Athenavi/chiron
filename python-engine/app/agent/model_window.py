"""模型上下文窗口表与 token 计量（C4 / C2 段 a）。

**阈值不该硬编码**：`runtime.py` 的 `MAX_CONTEXT_TOKENS = 8192` 是历史遗留（注释自陈
"约 GPT-4 的 1/10"），而 `llm_models.context_window` 列**早已存在**（baseline 迁移）。
本模块把它读进进程内缓存，作为压缩阈值的来源。

**为什么不引 tiktoken**（评审 01 §1.7 的三条理由）：

1. 它只覆盖 OpenAI 系词表，而 Chiron 声明 20+ provider —— anthropic/glm/qwen 等会系统性偏差；
2. 新增重依赖要付维护成本（`requirements.txt` 有过"声明缺口导致 CI 红"的教训）；
3. provider 已在响应里回传真实 `usage`，**用它校准比换词表更准**。

缓存策略：进程内 TTL（默认 5 分钟）。窗口配置属"低频变更、高频读取"，每次请求查库不值当；
TTL 内变更最多滞后 5 分钟 —— 这是刻意的取舍（与评审 01 §1.7 的"系数不落 PG"同一取向）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: 窗口配置的进程内缓存 TTL（秒）
WINDOW_CACHE_TTL_SECONDS = 300
#: 查不到窗口时的回落值（与 `runtime.MAX_CONTEXT_TOKENS` 同值，保持历史行为）
DEFAULT_CONTEXT_WINDOW = 8192
#: 触发压缩的窗口占比（与 deepagents 的 `fraction` 档对齐，便于横向调参）
COMPACT_TRIGGER_RATIO = 0.85
#: 压缩后保留的窗口占比
COMPACT_KEEP_RATIO = 0.10

_cache: dict[str, tuple[int, float]] = {}


async def context_window(model: str) -> int:
    """取该模型的上下文窗口（进程内缓存，缺失回落 `DEFAULT_CONTEXT_WINDOW`）。"""
    if not model:
        return DEFAULT_CONTEXT_WINDOW
    now = time.time()
    hit = _cache.get(model)
    if hit is not None and now - hit[1] < WINDOW_CACHE_TTL_SECONDS:
        return hit[0]

    window = await _query_window(model)
    _cache[model] = (window, now)
    return window


def cached_window(model: str) -> int | None:
    """同步读缓存（不查库）；未命中返回 `None`。

    供"不想 await"的调用点使用 —— 它们宁可回落常量，也不该为读窗口阻塞。
    """
    hit = _cache.get(model)
    if hit is None:
        return None
    if time.time() - hit[1] >= WINDOW_CACHE_TTL_SECONDS:
        return None
    return hit[0]


def remember_window(model: str, window: int) -> None:
    """写入缓存（供测试与"已知窗口"的场景直接注入）。"""
    if model and window > 0:
        _cache[model] = (window, time.time())


def clear_cache() -> None:
    """清空缓存（测试用）。"""
    _cache.clear()


async def _query_window(model: str) -> int:
    """查 `llm_models.context_window`。

    `enabled IS NOT FALSE`：NULL 视为启用（历史数据里该列可为 NULL）。
    任何异常都回落默认值 —— 窗口查询失败不该阻断对话（它是"调参输入"，不是"安全边界"）。
    """
    try:
        from app.db import get_pool

        pool = get_pool()
        if pool is None:
            return DEFAULT_CONTEXT_WINDOW
        row = await pool.fetchrow(
            """
            SELECT context_window
              FROM llm_models
             WHERE name = $1 AND enabled IS NOT FALSE
             ORDER BY updated_at DESC NULLS LAST
             LIMIT 1
            """,
            model,
        )
        value = row["context_window"] if row is not None else None
        if value and int(value) > 0:
            return int(value)
    except Exception as exc:  # noqa: BLE001 — 查不到就用回落值
        logger.debug("context window lookup failed (%s): %s", model, exc)
    return DEFAULT_CONTEXT_WINDOW


class TokenEstimator:
    """字符近似 + provider usage **校准**（C4）。

    校准系数 = 真实 `input_tokens` ÷ （字符数 / 4），按 model 缓存并做指数平滑。
    这样不引词表也能逼近各家的真实分词密度 —— 而且用的是**真实回传值**，不是猜。

    异常观测不参与（`0.1 ~ 10.0` 之外）：一次坏数据不该把系数带偏，
    那会让后续所有阈值判断跟着错。
    """

    SMOOTHING = 0.3
    #: 字符→token 的初值（英文约 4、中文约 1.5；校准会在前几轮把它拉到位）
    CHARS_PER_TOKEN = 4.0
    MIN_OBSERVED_SCALE = 0.1
    MAX_OBSERVED_SCALE = 10.0

    def __init__(self) -> None:
        self._scales: dict[str, float] = {}

    @staticmethod
    def _chars(messages: list[dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            try:
                total += len(json.dumps(message, ensure_ascii=False))
            except (TypeError, ValueError):
                total += len(str(message))
        return total

    def estimate(self, messages: list[dict[str, Any]], *, model: str) -> int:
        """估算 token 数（字符近似 × 该 model 的校准系数）。"""
        return int(self._chars(messages) / self.CHARS_PER_TOKEN * self.scale(model))

    def calibrate(
        self, messages: list[dict[str, Any]], *, model: str, actual_input_tokens: int
    ) -> None:
        """用 provider 回传的真实 `input_tokens` 校准系数（指数平滑）。"""
        chars = self._chars(messages)
        if actual_input_tokens <= 0 or chars <= 0:
            return
        observed = actual_input_tokens / (chars / self.CHARS_PER_TOKEN)
        if not (self.MIN_OBSERVED_SCALE <= observed <= self.MAX_OBSERVED_SCALE):
            logger.debug(
                "token scale observation out of range (model=%s observed=%.2f)", model, observed
            )
            return
        current = self._scales.get(model, 1.0)
        self._scales[model] = current * (1 - self.SMOOTHING) + observed * self.SMOOTHING

    def scale(self, model: str) -> float:
        """当前系数（供 Prometheus 指标暴露，便于发现个别 provider 的异常偏差）。"""
        return self._scales.get(model, 1.0)


#: 图片 token 的经验估算系数：宽×高 / 750（与 Reasonix 的 `requestImageTokens` 同值）。
#: 没有 provider 会在 usage 返回前说清一张图值多少 token，这个系数对 `image_fit.Fit`
#: 夹过的图偏高，正合需要 —— 估高让窗口早压，估低则溢出。
_IMAGE_TOKENS_DIVISOR = 750


def image_tokens(messages: list[dict[str, Any]]) -> int:
    """估算这些消息里的图片会占多少 token。

    只读图片**头**（`Image.open` 的 `.size`），不整图解码；尺寸读不出来的（网络 URL、
    坏字节）按 `image_fit.MAX_DIM` 计满。图片不进字符估算（base64 是字符，`chars/4`
    会把一张图算成几万 token 或几十 token 都说不准），所以单独走这里。
    """
    from app.media.image_fit import MAX_DIM

    total = 0
    for url in _iter_image_urls(messages):
        size = _image_size(url)
        if size is None:
            total += (MAX_DIM * MAX_DIM + _IMAGE_TOKENS_DIVISOR - 1) // _IMAGE_TOKENS_DIVISOR
            continue
        width, height = size
        total += (width * height + _IMAGE_TOKENS_DIVISOR - 1) // _IMAGE_TOKENS_DIVISOR
    return total


def _iter_image_urls(messages: list[dict[str, Any]]) -> list[str]:
    """收集消息里的 `image_url` 值（内容可能是 str，也可能是 part 列表）。"""
    urls: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else None
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _image_size(url: str) -> tuple[int, int] | None:
    """只读头的尺寸；非 data URL 或读不出时返回 ``None``（调用方按 MAX_DIM 计）。"""
    if not url.startswith("data:"):
        return None
    _, _, payload = url.partition(";base64,")
    if not payload:
        return None
    try:
        import base64
        import io

        from PIL import Image

        # 头部通常在前几十 KB 内；多给一点以覆盖大 PNG 的 IHDR 之后的分块。
        head = base64.b64decode(payload[: 96 * 1024], validate=False)
        with Image.open(io.BytesIO(head)) as image:
            return int(image.size[0]), int(image.size[1])
    except Exception:  # noqa: BLE001 - 读不出头不是错误，按最大尺寸估
        return None


#: 进程内单例：校准系数跨回合累积才有意义（单次观测不足以定系数）
_estimator = TokenEstimator()


def estimator() -> TokenEstimator:
    return _estimator
