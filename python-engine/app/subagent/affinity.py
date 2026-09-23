"""子 Agent 作业 → 引擎实例的归属映射（P4）。

为什么需要：后台子 Agent 的 ``asyncio`` 任务跑在「父 turn 所在实例」的进程里，而网关
**没有 run → 实例的映射**，取消只能发一条 Redis 广播，由恰好持有该 run 的实例执行。于是：

* 实例在取消前重启/被驱逐 → 广播无人认领，而网关**仍然返回 ``accepted``**（假成功），
  DB 里这行会一直停在 ``running``，直到被收口器判为 ``lost``（默认 2 小时）；
* 用户看到的是"点了停止，一直转圈"，而系统认为"已经通知过了"。

本模块把「谁持有这个 run」写进 Redis：

    {REDIS_KEY_PREFIX}subagent:run:{run_id} = {"instance_id","url","token","session_id",
                                              "tenant_id","started_at","last_seen"}

网关据此判断"这个作业是否真的有人在跑"，并且**回执**（引擎认领并真的发出取消后
写入 ``subagent:cancel:ack:{run_id}``）让它能区分：

* 等到回执 → 取消确实生效（返回 ``accepted``）；
* 等不到回执 → 无人认领，**不再假装成功**：直接判 ``lost`` 并把真实结果返回前端。

与 ``app/run_registry.py``（``engine:run:{session}``，TTL 300s）的区别：那是「正在进行的一轮
对话」，TTL 只需覆盖一个 turn；这是「一个后台作业」，可能跑很久，所以 TTL 长得多
（默认 6h）并由心跳续期。键与生命周期都不同，**刻意不复用同一个对象** —— 复用会让
"一轮对话结束"顺手删掉"仍在跑的作业"的登记。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid

logger = logging.getLogger(__name__)

#: 归属键前缀（与 Go 侧 internal/engine/run_affinity.go 的 subagentRunPrefix 逐字一致）
OWNER_KEY_PREFIX = "subagent:run:"
#: 取消回执键前缀（与 Go 侧 internal/api/subagent_cancel.go 的 cancelAckKey 逐字一致）
ACK_KEY_PREFIX = "subagent:cancel:ack:"

#: 归属 TTL：必须显著长于单 run 的最长运行时间（SUBAGENT_MAX_RUNTIME 默认 1800s），
#: 否则一个跑得久的作业会在中途"失去归属"，被网关误判为无人认领。
DEFAULT_OWNER_TTL = 6 * 3600
#: 心跳周期：TTL 的 1/6，保证 TTL 窗口内至少续期多次（Redis 抖动不至于丢归属）。
OWNER_HEARTBEAT_SECONDS = 600
#: 回执 TTL：网关只等几秒；留一点余量给网络往返，避免键泄漏。
ACK_TTL_SECONDS = 120

_RELEASE_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then
  return 0
end
local ok, data = pcall(cjson.decode, cur)
if ok and data['token'] == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def instance_id() -> str:
    """本实例标识（**唯一实现**；``main.py`` 的 ``_get_instance_id`` 只是转发到这里）。

    环境变量 ``INSTANCE_ID`` 优先，其次 ``POD_NAME``，最后 ``hostname-随机后缀``。同一次
    进程内必须稳定（重启换名是预期行为：新实例就是新归属）。
    """
    from app.config import settings

    if settings.instance_id:
        return settings.instance_id
    if settings.pod_name:
        return settings.pod_name
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


_INSTANCE_CACHE = ""


def cached_instance_id() -> str:
    """进程内缓存的实例标识。

    为什么缓存：归属登记在每次子 Agent 启动时都会写，而 ``instance_id()`` 的兜底分支带
    随机后缀 —— 不缓存的话**同一个进程每次登记都会换一个实例名**，网关的映射会自相矛盾。
    """
    global _INSTANCE_CACHE
    if not _INSTANCE_CACHE:
        _INSTANCE_CACHE = instance_id()
    return _INSTANCE_CACHE


def owner_key(run_id: str) -> str:
    from app.redis_keys import rkey

    return rkey(OWNER_KEY_PREFIX) + run_id


def ack_key(run_id: str) -> str:
    from app.redis_keys import rkey

    return rkey(ACK_KEY_PREFIX) + run_id


def owner_ttl() -> int:
    """归属 TTL（秒）；``SUBAGENT_OWNER_TTL_SECONDS<=0`` 表示退回默认值。"""
    raw = (os.getenv("SUBAGENT_OWNER_TTL_SECONDS") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_OWNER_TTL
    except ValueError:
        value = DEFAULT_OWNER_TTL
    return value if value > 0 else DEFAULT_OWNER_TTL


def _payload(*, run_id: str, token: str, session_id: str, tenant_id: str) -> str:
    from app.config import settings

    return json.dumps({
        "instance_id": cached_instance_id(),
        "url": getattr(settings, "engine_advertise_url", "") or "",
        "token": token,
        # 用 run_id 自身作为"内容指纹"：归属键本来就按 run 唯一，这里额外带上便于排查。
        "run_id": run_id,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


async def get_owner_redis():
    """取归属用的 Redis 客户端；不可用时返回 None（登记失败只降级，不阻断子 Agent）。"""
    try:
        from app.redis_client import get_redis

        return await get_redis()
    except Exception as exc:  # noqa: BLE001
        logger.debug("subagent affinity: redis unavailable: %s", str(exc)[:160])
        return None


async def publish_owner(
    redis,
    run_id: str,
    *,
    token: str,
    session_id: str = "",
    tenant_id: str = "",
) -> bool:
    """写入（或续期）一次作业归属。返回是否成功。"""
    if redis is None or not run_id:
        return False
    try:
        await redis.set(
            owner_key(run_id),
            _payload(run_id=run_id, token=token, session_id=session_id, tenant_id=tenant_id),
            ex=owner_ttl(),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 归属写入失败不该影响子 Agent
        logger.warning("subagent affinity publish failed: run=%s err=%s", run_id, str(exc)[:160])
        return False


async def release_owner(redis, run_id: str, token: str = "") -> None:
    """注销归属（带 token 校验：迟到的清理不会删掉同 run 的新登记）。"""
    if redis is None or not run_id:
        return
    try:
        await redis.eval(_RELEASE_LUA, 1, owner_key(run_id), token)
    except Exception as exc:  # noqa: BLE001
        logger.debug("subagent affinity release failed: run=%s err=%s", run_id, str(exc)[:160])


async def owner_of(redis, run_id: str) -> dict | None:
    """读取归属记录（诊断/测试用；网关侧读同一份键）。"""
    if redis is None or not run_id:
        return None
    try:
        raw = await redis.get(owner_key(run_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("subagent affinity read failed: %s", str(exc)[:160])
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


async def ack_cancel(redis, run_id: str) -> bool:
    """写下"本实例认领并已发出取消"的回执（网关 BLPOP 取走）。

    这是网关区分"真取消"与"广播无人应答"的**唯一**依据 —— 没有它，取消只能在
    "我们发出去了"这一层返回成功，而这正是"重启后取消落空却返回成功"的成因。
    """
    if redis is None or not run_id:
        return False
    try:
        await redis.lpush(ack_key(run_id), "1")
        await redis.expire(ack_key(run_id), ACK_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 - 回执失败只影响网关的判定精度
        logger.warning("subagent cancel ack failed: run=%s err=%s", run_id, str(exc)[:160])
        return False


class OwnerLease:
    """一个子 Agent run 的归属租约：启动即登记，周期续期，结束注销。

    用法与 ``app/run_registry.RunLease`` 一致（但键、TTL 与语义不同）：

        lease = OwnerLease(run_id, session_id=..., tenant_id=...)
        await lease.start()
        try:
            ...
        finally:
            await lease.stop()
    """

    def __init__(
        self,
        run_id: str,
        *,
        session_id: str = "",
        tenant_id: str = "",
        redis=None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._run_id = run_id
        self._session_id = session_id
        self._tenant_id = tenant_id
        self._redis = redis
        self._token = uuid.uuid4().hex
        self._ttl = ttl_seconds
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._run_id)

    async def start(self) -> bool:
        """登记归属并启动续期循环；Redis 不可用时静默降级（返回 False）。"""
        if not self.enabled:
            return False
        if self._redis is None:
            self._redis = await get_owner_redis()
        ok = await publish_owner(
            self._redis,
            self._run_id,
            token=self._token,
            session_id=self._session_id,
            tenant_id=self._tenant_id,
        )
        if ok and self._task is None:
            self._task = asyncio.create_task(self._loop())
        return ok

    async def stop(self) -> None:
        """停止续期并注销归属（失败只记日志：调用方通常在收尾路径上）。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 续期循环的异常不该影响收尾
                pass
            self._task = None
        await release_owner(self._redis, self._run_id, self._token)

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(OWNER_HEARTBEAT_SECONDS)
                if self._redis is None:
                    self._redis = await get_owner_redis()
                await publish_owner(
                    self._redis,
                    self._run_id,
                    token=self._token,
                    session_id=self._session_id,
                    tenant_id=self._tenant_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 续期失败不阻断 run
            logger.warning("subagent affinity heartbeat failed: run=%s err=%s",
                           self._run_id, str(exc)[:160])


async def release_owners(run_ids: list[str]) -> int:
    """批量注销归属（关机时用），返回成功注销的数量。

    P4-3：进程退出后这些 run 的收尾代码不会再执行 —— 留着归属会让网关以为"有人在跑"
    并把取消路由到一个正在退出的实例；注销后网关能明确判定"无人认领"并收敛为 ``lost``。
    """
    if not run_ids:
        return 0
    redis = await get_owner_redis()
    if redis is None:
        return 0
    released = 0
    for run_id in run_ids:
        try:
            # 关机路径不校验 token（进程要走了，键留着只会误导网关）
            await redis.delete(owner_key(run_id))
            released += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("subagent affinity release-all failed: run=%s err=%s",
                         run_id, str(exc)[:160])
    if released:
        logger.info("subagent affinity: released %d owned run(s) on shutdown", released)
    return released
