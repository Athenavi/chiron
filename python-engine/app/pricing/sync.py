"""按模型的公开价格同步（M28）。

**要解决什么**：本仓的单价是**全局**的（`BillingConfig.LLMCostPerToken/Output`，1 / 2
credits per 1000 tokens），对所有模型一视同仁 —— 而不同模型的价格差几个数量级。成本核算、
`cost` 轴的单价、按成本选模型都需要**按模型**的价格，这张表（`model_prices`，迁移 `0029`）存它。

**数据从哪来**：公开的价格数据集（无需认证）。多源**交叉验证**：同一个模型至少两个源给出
**相对差 ≤ 5%** 的价格才写入；不一致 ⇒ 记分歧、**不写入**、打点。宁可没有数据，也不要一个
来源不明或自相矛盾的数字。

⚠ **跨源模型名对齐是启发式**（见 :func:`normalize_model`）：两个源对同一个模型的叫法可能差得远，
那种情况这里对齐不上 ⇒ 结果是"该模型只有一个源" ⇒ 判它未验证、不写入。这是**有意的保守**：
拿两个不同模型的价格互相"验证"，比没有数据更糟。

⚠ **它不参与计费**：计费权威是 `BillingConfig`（管理员可改、`billing_records` 按它记账）。
这张表是参考数据 —— 混进计费会让"谁说了算"不可追溯。

**为什么是定时拉取而不是运行时查询**：价格是慢变量（日更），而运行时查询会把外部依赖放进
请求路径（出站面 + 延迟 + 失败降级）。定时拉取把不确定性收在一个后台任务里，且默认**关**
（`MODEL_PRICE_SYNC`）—— 新增机制默认什么都不做。

**出站必须走 `fetch_url_safe`**（`app/tools/ssrf.py`）：本仓纪律是 9 个既有出站调用点全走它。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from app.observability.metrics import MODEL_PRICE_SYNC

logger = logging.getLogger(__name__)

#: 价格**统一口径**：USD / 1000 tokens。各源单位不同（portkey = 美分/token；llmrates =
#: 每百万 token + 原生货币）⇒ 每个源的解析函数自己负责换算到这一个口径，下游不再做单位转换。
UNIT = "USD/1k"

#: 交叉验证的默认容差（相对差）。5% 是"同一个价格在不同源上的正常浮动"的量级：
#: 再严会把四舍五入差异判成分歧，再松就拦不住真的不同价（例如某源的旧价）。
DEFAULT_TOLERANCE = 0.05

DEFAULT_INTERVAL_SECONDS = 1800  # 30min（用户拍板）

#: 源的目录：(名字, URL)。URL 必须能经 `assert_safe_url`（公网 HTTPS）。
SOURCES: tuple[tuple[str, str], ...] = (
    ("portkey", "https://configs.portkey.ai/pricing/openai.json"),
    ("llmrates", "https://www.llmrates.ai/api/dataset"),
)


@dataclass(frozen=True)
class ModelPrice:
    """一个模型的一条价格（已归一化到 :data:`UNIT`）。"""

    model: str
    provider: str
    input_per_1k: float
    output_per_1k: float


def normalize_model(name: str) -> str:
    """跨源对齐模型名的**启发式**：小写、去分隔符、去日期后缀。

    ⚠ 这不是权威映射。两个源对同一个模型的叫法可能差得远（`glm-4v` vs `glm-4-vision`），
    这里**对齐不上** —— 那种情况下游会判"只有一个源"、不写入。**宁可缺数据，也不要拿两个
    不同模型的价格互相"验证"**（那会造出一个来源不明的数字，比没有更糟）。
    """
    lowered = name.strip().lower()
    for sep in ("_", " ", ".", "/"):
        lowered = lowered.replace(sep, "-")
    parts = [p for p in lowered.split("-") if p]
    # 去掉纯数字的版本段（`-20241022` / `-0613` 这类日期戳）
    while parts and parts[-1].isdigit():
        parts.pop()
    return "-".join(parts)


def _to_float(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def parse_portkey(payload: dict[str, Any], *, provider: str = "openai") -> dict[str, ModelPrice]:
    """Portkey 的定价 JSON：``{model: {pricing_config: {pay_as_you_go: {...}}}}``。

    单位是**美分 / token**（文档自陈）⇒ 换算：``usd_per_1k = price_cents * 10``
    （1 美分 = 0.01 美元；per token → per 1k 乘 1000 ⇒ 0.01 × 1000 = 10）。
    """
    out: dict[str, ModelPrice] = {}
    for model, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        payg = (entry.get("pricing_config") or {}).get("pay_as_you_go") or {}
        req = _to_float((payg.get("request_token") or {}).get("price"))
        resp = _to_float((payg.get("response_token") or {}).get("price"))
        if req <= 0 and resp <= 0:
            continue
        out[normalize_model(str(model))] = ModelPrice(
            model=str(model), provider=provider, input_per_1k=req * 10, output_per_1k=resp * 10
        )
    return out


def parse_llmrates(payload: dict[str, Any]) -> dict[str, ModelPrice]:
    """LLMRates 的数据集：``{"models": [{name, provider:{slug}, prices:[{...}]}]}``。

    单位是**每百万 token + 原生货币**（`priceUnit`）⇒ 换算：``usd_per_1k = per_million / 1000``。
    ⚠ **非 USD 的条目一律跳过**：本仓不做汇率换算（汇率是另一个会漂移的外部量，且价格表不参与
    计费，没有必须换算的压力）。跳过时打点，让"某个源大部分是 CNY"这件事可见。
    """
    out: dict[str, ModelPrice] = {}
    skipped_currency = 0
    for entry in payload.get("models") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        provider = str((entry.get("provider") or {}).get("slug") or "")
        for price in entry.get("prices") or []:
            if not isinstance(price, dict):
                continue
            if str(price.get("priceUnit") or "USD").upper() != "USD":
                skipped_currency += 1
                continue
            inp = _to_float(price.get("inputPricePerMillion"))
            outp = _to_float(price.get("outputPricePerMillion"))
            if inp <= 0 and outp <= 0:
                continue
            out[normalize_model(name)] = ModelPrice(
                model=name, provider=provider, input_per_1k=inp / 1000, output_per_1k=outp / 1000
            )
            break  # 一个模型取第一条 USD 价格（后面的通常是其它 tier/模态）
    if skipped_currency:
        MODEL_PRICE_SYNC.labels(outcome="skipped_currency", source="llmrates").inc(skipped_currency)
    return out


#: 源名 → 解析函数。加新源只需在这里加一行 + 一个解析函数（各自负责换算到 UNIT）。
PARSERS: dict[str, Any] = {"portkey": parse_portkey, "llmrates": parse_llmrates}


def _close_enough(a: ModelPrice, b: ModelPrice, tolerance: float) -> bool:
    """两条价格在容差内一致（**两轴都要**：只比输入会让"输出价差一倍"溜过去）。"""
    for x, y in ((a.input_per_1k, b.input_per_1k), (a.output_per_1k, b.output_per_1k)):
        scale = max(abs(x), abs(y), 1e-9)
        if abs(x - y) / scale > tolerance:
            return False
    return True


def cross_validate(
    per_source: dict[str, dict[str, ModelPrice]], *, tolerance: float = DEFAULT_TOLERANCE
) -> tuple[list[ModelPrice], list[str]]:
    """多源比对：至少**两个源**给出容差内一致的价格才收。

    返回 ``(可写入的价格, 分歧描述)``。分歧**不写入**（见模块头：宁可缺数据）—— 但也**不静默**：
    描述会进日志与指标，否则"价格表一直是空的"没人知道为什么。
    """
    by_model: dict[str, list[tuple[str, ModelPrice]]] = {}
    for source, prices in per_source.items():
        for norm, price in prices.items():
            by_model.setdefault(norm, []).append((source, price))

    agreed: list[ModelPrice] = []
    disagreements: list[str] = []
    for norm, entries in sorted(by_model.items()):
        if len(entries) < 2:
            # 只有一个源 —— **不写入**：单源无法交叉验证，写进去就等于给一个未验证的数字背书。
            MODEL_PRICE_SYNC.labels(outcome="single_source", source=entries[0][0]).inc()
            continue
        first_source, first = entries[0]
        if all(_close_enough(first, other, tolerance) for _, other in entries[1:]):
            agreed.append(first)
            MODEL_PRICE_SYNC.labels(outcome="agreed", source=first_source).inc()
            continue
        detail = ", ".join(f"{src}={p.input_per_1k:.4f}/{p.output_per_1k:.4f}" for src, p in entries)
        disagreements.append(f"{norm}: {detail}")
        MODEL_PRICE_SYNC.labels(outcome="disagreement", source=first_source).inc()
    return agreed, disagreements


async def _fetch_source(name: str, url: str, *, timeout: float = 20.0) -> dict[str, Any] | None:
    """拉一个源并解析。任何失败都返回 ``None``（调用方按"这个源这轮没数据"处理）。"""
    import httpx

    from app.tools.ssrf import fetch_url_safe

    parser = PARSERS.get(name)
    if parser is None:
        return None
    try:
        # follow_redirects=False 是 `fetch_url_safe` 的前置条件（它自己逐跳校验重定向）。
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            resp = await fetch_url_safe(client, url)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — 外部数据源的任何问题都不该打断同步循环
        MODEL_PRICE_SYNC.labels(outcome="fetch_failed", source=name).inc()
        logger.warning("model price fetch failed (%s): %s", name, str(exc)[:160])
        return None
    if not isinstance(payload, dict):
        MODEL_PRICE_SYNC.labels(outcome="bad_payload", source=name).inc()
        return None
    try:
        parsed = parser(payload)
    except Exception as exc:  # noqa: BLE001 — 源改了结构 = 解析失败，按"这轮没数据"处理
        MODEL_PRICE_SYNC.labels(outcome="parse_failed", source=name).inc()
        logger.warning("model price parse failed (%s): %s", name, str(exc)[:160])
        return None
    if not isinstance(parsed, dict):
        # 解析函数契约是"返回 {规范化名: ModelPrice}" —— 返回别的东西按坏载荷处理，
        # 而不是把 Any 透传给下游（下游会拿它当 dict 用）。
        MODEL_PRICE_SYNC.labels(outcome="bad_payload", source=name).inc()
        return None
    return parsed


async def sync_once() -> tuple[int, list[str]]:
    """拉全部源 → 交叉验证 → 写入。返回 ``(写入行数, 分歧描述)``。

    ⚠ **至少要有两个源成功**才做交叉验证：只有一个源时 :func:`cross_validate` 会把所有模型
    都判成 `single_source` ⇒ 一行都不写（这是**正确**的降级：宁可这轮不更新，也不写入未验证值）。
    """
    fetched: dict[str, dict[str, ModelPrice]] = {}
    for name, url in SOURCES:
        prices = await _fetch_source(name, url)
        if prices:
            fetched[name] = prices
    if not fetched:
        MODEL_PRICE_SYNC.labels(outcome="no_source", source="-").inc()
        return 0, []

    agreed, disagreements = cross_validate(fetched)
    if disagreements:
        # 不静默：分歧要么是源过期、要么是我们对齐错了 —— 两种都需要人看一眼。
        logger.warning("model price disagreements (%d): %s", len(disagreements), "; ".join(disagreements[:5]))

    written = await _upsert(agreed, sources=sorted(fetched))
    return written, disagreements


async def _upsert(prices: list[ModelPrice], *, sources: list[str]) -> int:
    """把验证过的价格写进 `model_prices`（表缺失 / 库不可用 ⇒ 记一次告警并放弃）。"""
    if not prices:
        return 0
    from app.db import get_pool  # 与其它 PG 访问同一条连接池入口

    pool = await get_pool()
    if pool is None:
        MODEL_PRICE_SYNC.labels(outcome="db_unavailable", source="-").inc()
        return 0
    src = ",".join(sources)
    written = 0
    try:
        async with pool.acquire() as conn:
            for price in prices:
                await conn.execute(
                    """
                    INSERT INTO model_prices
                        (model, provider, input_per_1k, output_per_1k, sources, source_count,
                         verified_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, now(), now())
                    ON CONFLICT (model, provider) DO UPDATE SET
                        input_per_1k = EXCLUDED.input_per_1k,
                        output_per_1k = EXCLUDED.output_per_1k,
                        sources = EXCLUDED.sources,
                        source_count = EXCLUDED.source_count,
                        verified_at = now(),
                        updated_at = now()
                    """,
                    price.model,
                    price.provider,
                    price.input_per_1k,
                    price.output_per_1k,
                    src,
                    len(sources),
                )
                written += 1
    except Exception as exc:  # noqa: BLE001 — 未迁移的库只该丢这张表，不该让任务崩
        MODEL_PRICE_SYNC.labels(outcome="write_failed", source="-").inc()
        logger.warning("model price upsert failed（请执行迁移 0029_model_prices）: %s", str(exc)[:160])
        return 0
    MODEL_PRICE_SYNC.labels(outcome="written", source="-").inc(written)
    return written


def sync_enabled() -> bool:
    """开关：``MODEL_PRICE_SYNC=on`` 才跑。**默认关** —— 新增机制默认什么都不做。"""
    return (os.getenv("MODEL_PRICE_SYNC") or "").strip().lower() in ("1", "on", "true", "yes")


def sync_interval_seconds() -> int:
    """同步间隔（秒），默认 1800（用户拍板 30min）。取不到或非法值回落默认。"""
    raw = (os.getenv("MODEL_PRICE_SYNC_INTERVAL_SECONDS") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return value if value >= 60 else DEFAULT_INTERVAL_SECONDS


async def start_price_sync() -> None:
    """后台循环：先等一个间隔再拉（启动瞬间不抢资源），之后每 ``interval`` 秒一轮。

    与 `main.py` 既有的 lifespan 循环同形（`while True: sleep; work`）。**未启用时直接返回** ——
    调用方可以无条件 await 它，不必自己判开关。
    """
    if not sync_enabled():
        return
    interval = sync_interval_seconds()
    logger.info("model price sync started (every %ss)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            written, disagreements = await sync_once()
            logger.info("model price sync: wrote %d rows, %d disagreements", written, len(disagreements))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 一轮失败不该让循环死掉
            MODEL_PRICE_SYNC.labels(outcome="cycle_failed", source="-").inc()
            logger.warning("model price sync cycle failed: %s", str(exc)[:160])
