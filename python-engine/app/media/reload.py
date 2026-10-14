"""媒体存储：随后台配置热切换（引擎侧）。

订阅网关广播的 `{prefix}chiron:settings:changed`；当分类为 `storage` 时，按广播携带的
storage 配置重建媒体后端并替换进程内当前后端（`app/media/store.py` 的 `set_store`）。

**为什么需要**：后台「存储」配置此前只对**网关**生效 —— 引擎的媒体后端由**环境变量**在
进程启动时确定（`create_store`），后台改动对引擎无效。结果同一个集群里网关写 S3、
引擎写容器本地盘，多副本下媒体"间歇性 404"。本订阅让引擎与网关对**同一份**后台配置即时一致。
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

#: ⚠️ 频道名必须与 Go 侧 `internal/api/settings_broadcast.go` 的 `settingsChangedChannel()`
#: 逐字相同（两侧各自经 key 前缀函数加 `REDIS_KEY_PREFIX`；前缀不一致时监听自然失效）。
SETTINGS_CHANGED_CHANNEL = "chiron:settings:changed"

#: 只处理「存储」分类，其余分类与本监听无关。
STORAGE_CATEGORY = "storage"

#: 订阅断开后的重连退避（秒）
RECONNECT_DELAY_SECONDS = 5.0


def apply_storage_config(config: dict[str, object] | None) -> bool:
    """按后台 storage 配置热切换媒体后端；返回是否切换成功。

    失败（配置不合法 / 构造后端报错）时**保持现状**并记 warning —— 绝不因一次坏配置把
    正在服务的后端打掉。
    """
    if not config:
        return False
    from app.media.store import set_store, store_from_config

    try:
        store = store_from_config(dict(config))
    except Exception as exc:  # noqa: BLE001 - 坏配置不能拖垮监听循环
        logger.warning("storage config invalid; keeping current media backend: %s", exc)
        return False
    set_store(store)
    logger.info(
        "media backend hot-switched from settings: backend=%s bucket=%s",
        config.get("backend"),
        config.get("s3_bucket") or "-",
    )
    return True


def _handle_message(raw: object) -> None:
    try:
        payload = json.loads(raw if isinstance(raw, str) else str(raw))
    except Exception:
        logger.warning("settings broadcast unmarshal failed")
        return
    if not isinstance(payload, dict):
        return
    if payload.get("category") != STORAGE_CATEGORY:
        return
    cfg = payload.get("config")
    apply_storage_config(cfg if isinstance(cfg, dict) else None)


async def listen_for_storage_changes() -> None:
    """订阅设置变更广播；storage 分类到达即热切换媒体后端。

    **失败不致命**：Redis 不可用 / 断连时记 warning 并退避重试；lifespan 把它当周期任务取消
    （取消会让 `CancelledError` 冒泡，因此停机不会卡在这里）。
    """
    from app.redis_keys import rkey

    channel = rkey(SETTINGS_CHANGED_CHANNEL)
    while True:
        pubsub = None
        try:
            from app.redis_client import aclose_pubsub, get_redis

            redis = await get_redis()
            if redis is None:
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                continue
            pubsub = redis.pubsub()
            await pubsub.subscribe(channel)
            logger.info("storage settings listener subscribed: %s", channel)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                _handle_message(message.get("data"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 订阅断了要能自己回来
            logger.warning("storage settings listener error, retrying: %s", exc)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
        finally:
            if pubsub is not None:
                try:
                    await aclose_pubsub(pubsub)
                except Exception:  # noqa: BLE001 - 关连接失败不该盖住上面的异常
                    logger.debug("storage pubsub close failed", exc_info=True)
