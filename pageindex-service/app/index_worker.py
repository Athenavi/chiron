"""pageindex-worker —— 消费 ``engine:tasks:kb`` 的索引执行者（S4）。

与引擎 worker 的关系：**同一条 Redis Stream 协议、不同消费组**。kb 任务单独走
``engine:tasks:kb`` 是因为 Redis Stream 单消费组语义下"引擎 worker 别碰 kb_index"
不能靠过滤（消息一旦被取走就在它手里了），只能换队列（见引擎侧 app/queue/streams.py）。

它做四件事，每件都只做一次、且必须幂等（重投/崩溃重启都会再跑）：

1. 读 KB 行与用户设置（Cloud key 由 ``APP_SECRET`` 解密；**不起用引擎的代码**，见 runtime.py）；
2. 准备索引位置（Cloud 建 folder / local 用共享卷目录）并回写 ``index_ref``；
3. 逐文档读原文 → ``submit_document`` → 回写 ``pageindex_doc_id``/``page_count``；
4. 全部成功才把库置 ``ready`` 并记录 ``index_engine``（SDK 版本，升级后据此判断重建）。

**库级互斥**：多副本部署时同一库可能被两个 worker 同时取到。这里用 Redis ``SET NX``
做库级锁（带 TTL），拿不到锁就把消息留在 PEL（不 ACK），由后续认领重试 —— 与引擎
worker 的 reclaim 语义一致，不需要额外的调度器。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
from dataclasses import replace
from typing import Any

from app.adapter import Adapter, PageIndexError, RequestContext, sdk_version
from app.config import Config
from app.runtime import BlobReader, decrypt_secret, rkey
from app.store import Store

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
KB_LOCK_TTL = 3600  # 索引长任务的上限（单库串行；到期自动释放，避免崩溃后永久占锁）


# ── 单库索引（可单独测试，不依赖 Redis）────────────────────────────────────


async def handle_kb_index(
    *,
    cfg: Config,
    store: Store,
    reader: BlobReader,
    kb_id: str,
    user_id: str = "",
    force: bool = False,
    keepalive: Any = None,
) -> dict[str, Any]:
    kb = await store.get_kb(kb_id)
    if kb is None:
        raise RuntimeError(f"知识库不存在: {kb_id}")
    if str(kb["status"] or "") == "ready" and not force:
        # 幂等：任务重投/重复投递时库已就绪 → 不再重复索引（也不重复花 LLM 钱）。
        # force=True 是"重建"入口（升级 pageindex 后换掉旧索引必须走它）。
        return {"status": "skipped", "reason": "already ready"}

    owner_id = user_id or str(kb["user_id"] or "")
    mode = "cloud" if str(kb["provider"] or "") == "cloud" else "selfhosted"
    settings_row = await store.get_user_settings(owner_id) if owner_id else None

    api_key = ""
    cloud_folder = str((settings_row["cloud_folder_id"] if settings_row else "") or "")
    if mode == "cloud":
        enc = str((settings_row["cloud_api_key_enc"] if settings_row else "") or "")
        if not enc:
            detail = "未配置 PageIndex Cloud API key"
            await store.set_kb_status(kb_id, "error", detail)
            return {"status": "error", "detail": detail}
        try:
            api_key = decrypt_secret(enc, cfg.app_secret)
        except Exception as exc:  # noqa: BLE001 — 解不开就是配置事故，必须可见
            detail = f"Cloud key 无法解密（APP_SECRET 是否与引擎一致？）: {exc}"
            await store.set_kb_status(kb_id, "error", detail)
            return {"status": "error", "detail": detail}

    ctx = RequestContext(
        kb_id=kb_id,
        tenant_id=str(kb["tenant_id"] or ""),
        user_id=owner_id,
        index_ref=str(kb["index_ref"] or ""),
        user_folder_id=cloud_folder,
        api_key=api_key,
        llm=cfg.llm_backend() if mode == "selfhosted" else {},
    )
    adapter = Adapter(mode=mode, cfg=cfg)
    await store.set_kb_status(kb_id, "indexing")

    # 1) 位置（Cloud 的两层 folder / local 的租户-库目录）
    try:
        if mode == "cloud":
            kb_folder, user_folder = adapter.ensure_kb_folder(ctx)
            if kb_folder and kb_folder != ctx.index_ref:
                await store.set_kb_index_ref(kb_id, kb_folder)
                ctx = replace(ctx, index_ref=kb_folder)
            if user_folder and user_folder != ctx.user_folder_id:
                await store.set_cloud_folder(owner_id, user_folder)
                ctx = replace(ctx, user_folder_id=user_folder)
        else:
            await store.set_kb_index_ref(kb_id, adapter.storage_path(ctx))
    except Exception as exc:  # noqa: BLE001 — 位置没准备好就不该开始提交文档
        detail = f"索引位置准备失败: {exc}"
        await store.set_kb_status(kb_id, "error", detail)
        return {"status": "error", "detail": detail}

    # 2) 逐文档索引
    docs = await store.pending_docs(kb_id, force=force)
    errors: list[str] = []
    indexed = 0
    for doc in docs:
        doc_id = str(doc["id"])
        try:
            storage_key = str(doc["storage_key"] or "")
            if not storage_key:
                raise PageIndexError("文档缺少原文对象（storage_key 为空）")
            data = await asyncio.to_thread(reader.get, storage_key)
            await store.set_doc_indexing(doc_id)
            result = await asyncio.to_thread(
                adapter.submit_bytes,
                ctx,
                doc_id=doc_id,
                filename=str(doc["name"] or f"{doc_id}.pdf"),
                data=data,
            )
            await store.set_doc_ready(doc_id, result.doc_id, result.page_count)
            indexed += 1
            if keepalive is not None:
                # 大库（几千页、optimize=full）可能跑过锁 TTL：每完成一个文档续一次期，
                # 否则锁过期后第二个 worker 会并发索引同一库（双倍 LLM 费用）。
                await keepalive()
        except Exception as exc:  # noqa: BLE001 — 单文档失败不拖垮整库
            message = str(exc)[:500]
            errors.append(f"{doc['name']}: {message}")
            logger.warning("kb_index doc failed kb=%s doc=%s: %s", kb_id, doc_id, message)
            await store.set_doc_error(doc_id, message)

    final = "error" if errors else "ready"
    await store.set_kb_status(kb_id, final, "; ".join(errors))
    if not errors:
        await store.set_kb_index_engine(kb_id, sdk_version())
    logger.info("kb_index done kb=%s indexed=%d failed=%d", kb_id, indexed, len(errors))
    return {"status": final, "indexed": indexed, "failed": len(errors)}


# ── 队列消费 ─────────────────────────────────────────────────────────────

_LOCK_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

_LOCK_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _field(fields: dict[Any, Any], name: str, default: bytes = b"") -> bytes:
    value = fields.get(name.encode()) or fields.get(name)
    if value is None:
        return default
    return value if isinstance(value, bytes) else str(value).encode()


def _lock_key(kb_id: str) -> str:
    return rkey(f"engine:kb:lock:{kb_id}")


async def _try_lock(redis: Any, kb_id: str, token: str) -> bool:
    ok = await redis.set(_lock_key(kb_id), token, nx=True, ex=KB_LOCK_TTL)
    return bool(ok)


async def _renew_lock(redis: Any, kb_id: str, token: str) -> None:
    """续期库级锁（只在 token 仍是自己时）。失败只告警：最坏是锁过期，不是数据损坏。"""
    try:
        await redis.eval(_LOCK_RENEW_LUA, 1, _lock_key(kb_id), token, KB_LOCK_TTL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("renew kb lock failed kb=%s: %s", kb_id, exc)


async def _unlock(redis: Any, kb_id: str, token: str) -> None:
    try:
        await redis.eval(_LOCK_RELEASE_LUA, 1, _lock_key(kb_id), token)
    except Exception as exc:  # noqa: BLE001 — 释放失败只等 TTL 过期
        logger.warning("release kb lock failed kb=%s: %s", kb_id, exc)


async def process_one(
    *,
    cfg: Config,
    store: Store,
    reader: BlobReader,
    redis: Any,
    stream_id: str,
    fields: dict[Any, Any],
) -> None:
    """处理一条 kb 消息：库级锁 → 索引 → ACK / 重投 / 进 DLQ。"""
    raw = _field(fields, "payload", b"{}")
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        payload = {}
    kb_id = str(payload.get("kb_id") or "")
    user_id = str(payload.get("user_id") or "")
    retry = int(_field(fields, "retry_count", b"0") or 0)

    if not kb_id:
        await _to_dlq(redis, cfg, fields, "payload 缺少 kb_id")
        await redis.xack(cfg.kb_stream, cfg.kb_group, stream_id)
        return

    token = f"{socket.gethostname()}:{os.getpid()}:{stream_id}"
    if not await _try_lock(redis, kb_id, token):
        # 同一库已有 worker 在跑：不 ACK，留在 PEL 由认领逻辑稍后重试（库级串行）
        logger.info("kb_index deferred (locked) kb=%s", kb_id)
        return

    try:
        result = await handle_kb_index(
            cfg=cfg,
            store=store,
            reader=reader,
            kb_id=kb_id,
            user_id=user_id,
            force=bool(payload.get("force")),
            keepalive=lambda: _renew_lock(redis, kb_id, token),
        )
        await redis.xack(cfg.kb_stream, cfg.kb_group, stream_id)
        await _bump(redis, "processed")
        logger.info("kb_index acked kb=%s result=%s", kb_id, result)
    except Exception as exc:  # noqa: BLE001
        if retry + 1 >= MAX_RETRIES:
            await _to_dlq(redis, cfg, fields, str(exc))
            await redis.xack(cfg.kb_stream, cfg.kb_group, stream_id)
            logger.error("kb_index to DLQ kb=%s: %s", kb_id, exc)
        else:
            fields = dict(fields)
            fields[b"retry_count"] = str(retry + 1).encode()
            await redis.xadd(cfg.kb_stream, fields, maxlen=cfg.kb_stream_maxlen)
            await redis.xack(cfg.kb_stream, cfg.kb_group, stream_id)
            logger.warning("kb_index requeued kb=%s retry=%d: %s", kb_id, retry + 1, exc)
    finally:
        await _unlock(redis, kb_id, token)


async def _to_dlq(redis: Any, cfg: Config, fields: dict[Any, Any], error: str) -> None:
    entry = dict(fields)
    entry[b"error"] = str(error).encode()[:500]
    await redis.xadd(cfg.kb_dlq, entry, maxlen=10000)


async def _bump(redis: Any, name: str) -> None:
    """累计处理结果（供 /metrics 读取；统计失败不影响主流程）。"""
    try:
        await redis.hincrby(rkey("engine:kb:stats"), name, 1)
    except Exception:  # noqa: BLE001
        pass


async def _heartbeat(redis: Any) -> None:
    """worker 存活心跳（60s TTL）。/metrics 据此判断"有没有 worker 在跑"。"""
    try:
        await redis.set(
            rkey(f"engine:kb:worker:hb:{socket.gethostname()}:{os.getpid()}"), "1", ex=60
        )
    except Exception:  # noqa: BLE001
        pass


async def _ensure_group(redis: Any, cfg: Config) -> None:
    try:
        await redis.xgroup_create(cfg.kb_stream, cfg.kb_group, id="0", mkstream=True)
        logger.info("consumer group '%s' created on %s", cfg.kb_group, cfg.kb_stream)
    except Exception as exc:  # noqa: BLE001 — BUSYGROUP 表示已存在
        if "BUSYGROUP" not in str(exc):
            raise


async def _reclaim_loop(
    redis: Any, cfg: Config, consumer: str, *, store: Store, reader: BlobReader
) -> None:
    """周期性认领 idle 超阈值的 pending 消息（崩溃/被锁延后的任务由此复活）。"""
    while True:
        try:
            await asyncio.sleep(120)
            resp = await redis.xautoclaim(
                cfg.kb_stream, cfg.kb_group, consumer, min_idle_time=300_000, start_id="0-0", count=10
            )
            claimed = resp[1] if isinstance(resp, (list, tuple)) and len(resp) > 1 else []
            for item in claimed:
                stream_id, fields = item[0], item[1]
                retry = int(_field(fields, "retry_count", b"0") or 0)
                if retry >= MAX_RETRIES:
                    # 认领路径也必须能走到 DLQ：否则永远失败的消息会在 PEL 里被反复
                    # 认领、既不 ACK 也不进死信（引擎侧 worker 有同样的上限）。
                    await _to_dlq(redis, cfg, fields, "reclaim exceeded max retries")
                    await redis.xack(cfg.kb_stream, cfg.kb_group, stream_id)
                    await _bump(redis, "failed")
                    logger.error("reclaimed task to DLQ id=%s retry=%d", stream_id, retry)
                    continue
                logger.info("reclaimed kb task id=%s retry=%d", stream_id, retry)
                await process_one(
                    cfg=cfg,
                    store=store,
                    reader=reader,
                    redis=redis,
                    stream_id=stream_id,
                    fields=fields,
                )
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("kb reclaim loop error: %s", exc)


async def run() -> None:
    """入口：``python -m app.index_worker``。"""
    import redis.asyncio as aioredis

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    if not cfg.redis_url or not cfg.postgres_dsn:
        raise SystemExit("REDIS_URL / POSTGRES_DSN 未配置")

    store = Store(cfg.postgres_dsn)
    await store.connect()
    reader = BlobReader(cfg)

    redis = aioredis.from_url(cfg.redis_url)
    await _ensure_group(redis, cfg)
    consumer = f"pi-worker-{socket.gethostname()}-{os.getpid()}"
    logger.info("pageindex worker started: consumer=%s stream=%s", consumer, cfg.kb_stream)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    reclaim = asyncio.create_task(
        _reclaim_loop(redis, cfg, consumer, store=store, reader=reader)
    )
    # 不同库可并发（同库由库级锁串行）—— 这里兑现 PAGEINDEX_WORKER_CONCURRENCY。
    sem = asyncio.Semaphore(max(1, cfg.worker_concurrency))
    inflight: set[asyncio.Task[Any]] = set()
    try:
        while not stop.is_set():
            try:
                resp = await redis.xreadgroup(
                    cfg.kb_group, consumer, {cfg.kb_stream: ">"}, count=1, block=5000
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("xreadgroup failed: %s", exc)
                await asyncio.sleep(2)
                continue
            await _heartbeat(redis)
            for _stream, messages in resp or []:
                for stream_id, fields in messages:

                    async def _run(stream_id: str = stream_id, fields: Any = fields) -> None:
                        async with sem:
                            await process_one(
                                cfg=cfg,
                                store=store,
                                reader=reader,
                                redis=redis,
                                stream_id=stream_id,
                                fields=fields,
                            )

                    task = asyncio.create_task(_run())
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
    finally:
        reclaim.cancel()
        for task in list(inflight):
            task.cancel()
        await asyncio.gather(*inflight, return_exceptions=True)
        await store.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
