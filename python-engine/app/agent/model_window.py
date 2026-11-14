"""模型上下文窗口表与 token 计量。

**阈值不该硬编码**：`runtime.py` 的 `MAX_CONTEXT_TOKENS = 8192` 是历史遗留（注释自陈
"约 GPT-4 的 1/10"），而 `llm_models.context_window` 列**早已存在**（baseline 迁移）。
本模块把它读进进程内缓存，作为压缩阈值的来源。

**为什么不引 tiktoken**：

1. 它只覆盖 OpenAI 系词表，而 Chiron 声明 20+ provider —— anthropic/glm/qwen 等会系统性偏差；
2. 新增重依赖要付维护成本（`requirements.txt` 有过"声明缺口导致 CI 红"的教训）；
3. provider 已在响应里回传真实 `usage`，**用它校准比换词表更准**。

缓存策略：进程内 TTL（默认 5 分钟）。窗口配置属"低频变更、高频读取"，每次请求查库不值当；
TTL 内变更最多滞后 5 分钟 —— 这是刻意的取舍（"系数不落 PG"同一取向）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: 窗口配置的进程内缓存 TTL（秒）
WINDOW_CACHE_TTL_SECONDS = 300
#: 查不到窗口时的回落值（与 `compaction.MAX_CONTEXT_TOKENS` 同值，保持同口径）。
#:
#: 旧值 8192 会让**每个会话都在几轮内触顶**：压缩判定按这个数算比例，
#: 而现代模型窗口普遍 ≥128K。真正的小窗口模型应当在 `llm_models.context_window`
#: 里如实登记 —— 那是数据问题，不该用"给所有人一个极小兜底"来掩盖。
DEFAULT_CONTEXT_WINDOW = 128000
#: 查不到输出上限时的回落值（与 `settings.default_max_tokens` 同值，保持同一口径）
#:
#: 旧值 4096 是"整篇 write_file 被截断"的直接上游根因：一次调用吐不满 4k token 时，
#: 模型产出的 arguments 会在 JSON 中间被切断 → 参数非法 → 工具拒绝执行 → 重试又重演，
#: 长任务因此反复烧轮次却推不动。现代模型普遍支持 8k 以上输出，4096 只在旧模型才合理，
#: 而那种情况应当在 `llm_models.max_output_tokens` 里如实登记（数据问题，不该用
#: "给所有人一个极小兜底"来掩盖）。
DEFAULT_MAX_OUTPUT_TOKENS = 8192
#: 输出上限的下限。管理端填 1 会让模型一个字都说不完；低于此值按未知处理。
MIN_MAX_OUTPUT_TOKENS = 256
#: 触发压缩的窗口占比（与 deepagents 的 `fraction` 档对齐，便于横向调参）
COMPACT_TRIGGER_RATIO = 0.85
#: 压缩后保留的窗口占比
COMPACT_KEEP_RATIO = 0.10

_cache: dict[str, tuple[int, float]] = {}
#: 输出上限的独立缓存。**不与 `_cache` 合并**：两者是不同的量，同 key 会互相覆盖。
_max_output_cache: dict[str, tuple[int | None, float]] = {}


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


async def max_output_tokens(model: str, *, fallback: int = DEFAULT_MAX_OUTPUT_TOKENS) -> int:
    """取该模型的最大输出 token 数（进程内缓存，缺失回落 `fallback`）。

    **不从 `context_window` 推导**：两者是不同的量，同一窗口下差异可达 8 倍
    （deepseek-chat 8192 vs deepseek-reasoner 65536，窗口同为 131072）。
    这也是 LiteLLM / models.dev 把 `max_output_tokens` 与 `max_input_tokens`
    并列维护的原因。

    `fallback` 由调用方给（runtime 传 `settings.default_max_tokens`），缓存里只存
    **数据库里的原值**（含 `None`）—— 否则换一个 fallback 的调用方会读到别人缓存的
    回落值。

    返回值的语义是"这个模型单次最多能吐多少"，调用方应把它当作**上限**用
    （与请求方显式给的值取较小者），而不是直接当默认值 —— 用户显式要求短回复时
    不该被顶到模型的极限。
    """
    if not model:
        return fallback
    now = time.time()
    hit = _max_output_cache.get(model)
    if hit is not None and now - hit[1] < WINDOW_CACHE_TTL_SECONDS:
        return hit[0] if hit[0] is not None else fallback

    value = await _query_max_output(model)
    _max_output_cache[model] = (value, now)
    return value if value is not None else fallback


def remember_max_output(model: str, value: int) -> None:
    """写入输出上限缓存（供测试与"已知上限"的场景直接注入）。"""
    if model and value > 0:
        _max_output_cache[model] = (value, time.time())


def clear_cache() -> None:
    """清空缓存（测试用）。"""
    _cache.clear()
    _max_output_cache.clear()


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


async def _query_max_output(model: str) -> int | None:
    """查 `llm_models.max_output_tokens`；**查不到一律返回 `None`**（= 未知）。

    NULL / <=0 / 低于 `MIN_MAX_OUTPUT_TOKENS` 一律按"未知"处理：管理端误填 1 会让
    模型每一轮都说不出完整句子，视为未知比照单执行更安全（它是调参输入，不是安全边界）。

    任何异常同样返回 `None` —— 与窗口查询同一取向：查库失败不该阻断对话。
    `max_output_tokens` 列在旧库上可能不存在（迁移未跑），此时 asyncpg 抛
    UndefinedColumnError，正好走这条兜底路径，不需要额外探测 schema。

    **本函数不决定回落值**：调用方各有各的 fallback（runtime 传
    `settings.default_max_tokens`），在这里替它选一个会污染缓存 —— 见
    `max_output_tokens` 的说明。
    """
    try:
        from app.db import get_pool

        pool = get_pool()
        if pool is None:
            return None
        row = await pool.fetchrow(
            """
            SELECT max_output_tokens
              FROM llm_models
             WHERE name = $1 AND enabled IS NOT FALSE
             ORDER BY updated_at DESC NULLS LAST
             LIMIT 1
            """,
            model,
        )
        value = row["max_output_tokens"] if row is not None else None
        if value and int(value) >= MIN_MAX_OUTPUT_TOKENS:
            return int(value)
    except Exception as exc:  # noqa: BLE001 — 查不到就按"未知"处理
        logger.debug("max_output_tokens lookup failed (%s): %s", model, exc)
    return None


#: 内联媒体被折算成的占位符（只保留"这里曾有媒体"的信息，丢掉载荷字节）。
MEDIA_PLACEHOLDER = "<media>"
#: 判为"内联载荷"的最短长度。Anthropic 形态的 `{"type":"image","source":{"data":…}}`
#: 里 `data` 是裸 base64、不带 `data:` 前缀，只能按长度识别；256 足够避开正常短字段。
_INLINE_PAYLOAD_MIN_CHARS = 256


def strip_inline_media(value: Any) -> Any:
    """递归把内联媒体载荷折成占位符 —— **仅用于 token 估算**，不改变真正上行的消息。

    为什么必须折：图片的成本由 `image_tokens()` 按像素单独计，而 base64 的字符数既不是
    文本、也不是可信的 token 代理。实测一张 800×800 的 PNG（1.9MB）在字符口径下是
    64 万 token（应约 850），带一张图就足以让压缩判定认为"超窗 5 倍"——于是每次带图的
    请求都在错误的前提下触发压缩（或被上游拒绝）。两条估算路径（本模块的校准口径、
    `compaction._estimate_tokens` 的无 model 兜底）都必须走这里，否则两处口径又会分开。
    """
    if isinstance(value, str):
        return MEDIA_PLACEHOLDER if value.startswith("data:") else value
    if isinstance(value, list):
        return [strip_inline_media(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if (
                key == "data"
                and isinstance(item, str)
                and len(item) >= _INLINE_PAYLOAD_MIN_CHARS
            ):
                out[key] = MEDIA_PLACEHOLDER
                continue
            out[key] = strip_inline_media(item)
        return out
    return value


class TokenEstimator:
    """字符近似 + provider usage **校准**。

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
                # 先折掉内联媒体载荷：base64 的字符数不是 token 的代理（见 strip_inline_media）。
                payload = json.dumps(strip_inline_media(message), ensure_ascii=False)
            except (TypeError, ValueError):
                payload = str(message)
            total += len(payload)
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
