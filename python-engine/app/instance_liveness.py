"""实例存活心跳 —— 「这个引擎进程还在不在」的唯一真相源（M17）。

**要解决什么**（论文 §5.2 的线账一侧）：子 Agent 的在途预留记在 Redis 的 sorted set 里，
score 是到期时刻。崩溃（或 ``SIGKILL``）的 run 再也没机会 ``ZREM``，那条预留要等满
``RESERVE_TTL_SECONDS``（1h）才自然消失 —— 这期间这条线的预算被一个**已经不存在的执行体**
占着，新的派发会被误判成"线快满了"。要提前收回，就必须有一个比预留本身更可信的问题的答案：
**持有它的那个实例还活着吗**。本模块就是那个答案的来源。

**为什么不用现成的 ``engine:instance:*``**（批 E1 的实例注册表）：两个理由，第二个是硬的。

1. 它是**有条件**写的 —— ``EngineRegistry.enabled`` 要求 ``ENGINE_ADVERTISE_URL``。没配这个
   变量的部署里键根本不存在，拿它当存活判据的清扫会在这些环境**静默地永远判所有实例已死**
   （比不清扫更糟：它会把全站预留删空，而日志看起来一切正常）。
2. 它的值是给网关做地址路由用的（``{"url": …}``），语义是"可以被路由到"。存活心跳要的只是
   "进程还在"，两者一旦共用一个键，"退出中的实例"就会以空 URL 或陈旧 URL 出现在发现表里。

所以这里是一个**无条件、独立**的键：``{REDIS_KEY_PREFIX}engine:alive:{instance_id}``。
TTL/续期周期沿用批 E1 的 60s/20s 口径（同一处部署参数，两套键各自独立）。

**参与判定（``participates()``）是这条协议的安全阀**：只有"本进程确实已经开始写存活键"时，
才允许按"某个存活键读不到"去删别人的预留。反过来说，一个心跳没起来的实例如果照删不误，它
就会把全站的在途预留扫空 —— 方向是**少算**（上限失效），而少算正是线账最坏的一类静默。
读不到存活键只在一个前提下才是证据：这个进程自己也在协议里。

**自我豁免**：``holder == 本实例`` 直接算活，不查 Redis。否则本进程心跳键还没落（启动第一拍、
Redis 抖了一下）时，一次清扫会把自己正在跑的 run 的预留删掉 —— 那是自己把自己的闸门拆了。

参照实现：etcd 的 ``Lease`` + ``Session.Close()``（会话关闭即撤销挂在其下的键，见
``concurrency/session.go``）与 Kafka 消费者离开组时的 ``LeaveGroup``（让 coordinator 立刻
重分配，而不是等 ``session.timeout.ms``）—— 两者都是"**能告别的就主动告别**，剩下的靠 TTL
与存活判定"这一套组合，本模块负责"存活判定"那一半，另一半在 ``LineLedger.drain()``。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import Any

from app.redis_keys import rkey

logger = logging.getLogger(__name__)

#: 存活键 TTL 与续期周期：与批 E1 的注册表同一组数字（TTL 是周期的 3 倍，Redis 抖两次不至于丢）。
ALIVE_TTL_SECONDS = 60
ALIVE_INTERVAL_SECONDS = 20
#: "这个实例活着"的正向缓存时长。必须**远小于** TTL：缓存一条"活"的结论期间，该实例可能已经
#: 被杀掉而它自己的键还在 —— 那是清扫晚 ``ALIVE_PROBE_CACHE_SECONDS`` 才动手，代价有界。
#: 负向结论**不缓存**：读不到就是要删的那一刻，缓存它只会让同一批成员被反复删。
ALIVE_PROBE_CACHE_SECONDS = 10

#: 键前缀。**只有引擎侧读写**（网关不消费它），所以刻意不进
#: ``scripts/check_tool_policy_parity.py`` 的"跨语言共享前缀"族 —— 那个族比对的是两侧都有的
#: 字面量，这里加进去反而会要求一个不存在的 Go 常量。
ALIVE_KEY_PREFIX = "engine:alive:"


def alive_key(instance_id: str) -> str:
    return rkey(ALIVE_KEY_PREFIX) + instance_id


class InstanceLiveness:
    """单进程一个的心跳写入器（随 lifespan 启停，形状与 ``EngineRegistry`` 一致）。"""

    def __init__(self, redis: Any = None, instance_id: str = "") -> None:
        self._redis = redis
        self._instance_id = instance_id
        self._task: asyncio.Task[Any] | None = None
        self._started = False

    @property
    def redis(self) -> Any:
        return self._redis

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def started(self) -> bool:
        """存活键**当前**是否有人在续期。循环一死（Redis 一直连不上）就变 False ——
        清扫随之禁用：本进程已经没有资格判断别人的键为什么不在了。"""
        return self._started

    async def start(self) -> bool:
        """先**同步写一次**存活键，再起续期循环。

        先写这一次是必要的：心跳循环第一拍要等 ``ALIVE_INTERVAL_SECONDS``，而清扫可能在启动后
        几秒就被别的实例触发 —— 那时我的键还不存在，本进程的预留就会被自己的协议扫掉。
        返回 False = 没起来（无 Redis / 无实例 id / 写失败），调用方不必处理：清扫会因此
        自动禁用（``participates()``），这是设计好的退化方向。
        """
        if self._task is not None:
            return True
        if self._redis is None:
            self._redis = await _connect()
        if self._redis is None or not self._instance_id:
            logger.info(
                "instance liveness NOT started (redis=%s instance_id=%s)："
                "线账的死预留清扫将保持禁用，崩溃实例的预留只能等自然到期",
                self._redis is not None, bool(self._instance_id),
            )
            return False
        if not await self._publish():
            return False
        self._started = True
        _activate(self)
        self._task = asyncio.create_task(self._loop())
        return True

    async def stop(self) -> None:
        """停心跳并**主动删键**（优雅退出时不留下一个"还活着"的假象）。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 续期循环的异常不该阻断关机
                pass
            self._task = None
        if _ACTIVE is self:
            _deactivate(self)
        self._started = False
        if self._redis is not None:
            try:
                await self._redis.delete(alive_key(self._instance_id))
            except Exception as exc:  # noqa: BLE001
                logger.debug("instance liveness deregister failed: %s", str(exc)[:160])

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(ALIVE_INTERVAL_SECONDS)
                if self._redis is None:
                    self._redis = await _connect()
                    if self._redis is None:
                        return
                await self._publish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 心跳失败不该拖垮引擎
            logger.warning("instance liveness heartbeat loop stopped: %s", str(exc)[:160])
        finally:
            if _ACTIVE is self:
                _deactivate(self)
            self._started = False

    async def _publish(self) -> bool:
        try:
            await self._redis.set(
                alive_key(self._instance_id), str(int(time.time())), ex=ALIVE_TTL_SECONDS
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "instance liveness heartbeat write failed: %s（线账的死预留清扫保持禁用）",
                str(exc)[:160],
            )
            return False


#: 本进程**已在写存活键**的那个心跳实例。清扫读的就是这一份：它同时回答
#: "协议是否可依据"与"该用哪个 Redis 连接去问"。
_ACTIVE: InstanceLiveness | None = None


def _activate(hb: InstanceLiveness) -> None:
    global _ACTIVE
    _ACTIVE = hb


def _deactivate(hb: InstanceLiveness) -> None:
    """只在自己仍是当前活跃实例时让位 —— 一个已停用的旧实例不该把新心跳的登记抹掉。"""
    global _ACTIVE
    if _ACTIVE is hb:
        _ACTIVE = None


def participates() -> bool:
    """本进程是否已加入存活协议（见模块 docstring 的"参与判定"）。"""
    return _ACTIVE is not None and _ACTIVE.started


def self_id() -> str:
    """本进程的实例 id（豁免用：自己当然认自己活着）。"""
    return _ACTIVE.instance_id if _ACTIVE is not None else ""


async def _connect() -> Any:
    from app.redis_client import get_redis

    try:
        return await get_redis()
    except Exception as exc:  # noqa: BLE001 - 没配 Redis 是正常部署形态
        logger.debug("instance liveness redis unavailable: %s", str(exc)[:160])
        return None


_PROBE_CACHE: dict[str, float] = {}


async def holders_alive(holders: Iterable[str]) -> dict[str, bool]:
    """问这些实例还活着吗。``{}`` = 没问（协议未参与）⇒ 调用方**不得据此删任何东西**。

    一次 ``EXISTS`` 一个键：多键命令（``EXISTS k1 k2`` / ``MGET``）在 Redis Cluster 下会因
    跨槽报错 —— 线账这条读路径本来就规定"一个 EVAL 只碰一个键"，这里同一个规矩，
    而且实例数量就是个位数，逐键往返不是成本。
    """
    hb = _ACTIVE
    if hb is None or hb.redis is None:
        return {}
    redis = hb.redis
    mine = self_id()
    now = time.monotonic()
    out: dict[str, bool] = {}
    for holder in holders:
        if not holder:
            continue
        if holder == mine:
            out[holder] = True
            continue
        cached = _PROBE_CACHE.get(holder)
        if cached is not None and cached > now:
            out[holder] = True
            continue
        try:
            alive = bool(await redis.exists(alive_key(holder)))
        except Exception as exc:  # noqa: BLE001 - 问不出来就当活着（不清扫）
            logger.debug("liveness probe failed for %s: %s", holder, str(exc)[:160])
            out[holder] = True
            continue
        out[holder] = alive
        if alive:
            _PROBE_CACHE[holder] = now + ALIVE_PROBE_CACHE_SECONDS
        else:
            _PROBE_CACHE.pop(holder, None)
    return out
