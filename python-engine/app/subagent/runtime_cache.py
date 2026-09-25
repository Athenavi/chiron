"""子 Agent 运行期缓存（Redis）—— 让前端实时看到进度与层级。

数据分层（docs/subagent-design.md §3.3）：

* **Redis = 运行期**（TTL 1h，自动回收）：状态、摘要、事件流、子节点索引、整树骨架；
* **PostgreSQL = 权威**（``subagent_runs`` / ``subagent_run_steps``）：终态与完整过程。

key 约定（全部带 **tenant 维度**，多租户隔离是硬要求）::

    subagent:{tenant}:run:{run_id}         Hash   status/summary/depth/parent_run_id/profile/usage/updated_at
    subagent:{tenant}:ev:{run_id}          Stream 进度事件（MaxLen ≈ 500）
    subagent:{tenant}:children:{parent}    Set    子 run 索引（递归树懒加载）
    subagent:{tenant}:tree:{session}       Hash   run_id → 摘要 JSON（一次拉整棵树骨架）

写入点与 ``EventSink`` 一一对应：sink 已做限流/合并，因此写 Stream 不会放大。
Redis 不可用时全部静默降级（no-op），绝不影响子 Agent 执行。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.observability.metrics import SUBAGENT_PERSIST_FAILED
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

DEFAULT_TTL = 3600          # 运行期缓存 1h（与设计文档一致）
DEFAULT_MAX_EVENTS = 500    # 每 run 事件流上限


class SubagentRuntimeCache:
    """运行期缓存写入端（引擎侧直连 Redis）。"""

    def __init__(
        self, redis: Any = None, *, ttl: int = DEFAULT_TTL, max_events: int = DEFAULT_MAX_EVENTS
    ) -> None:
        self._redis = redis
        self._ttl = ttl
        self._max_events = max_events
        self._broken = False

    @property
    def available(self) -> bool:
        return self._redis is not None and not self._broken

    # ── key 构造（与 Go 侧 internal/api/subagent_handler.go 必须一致）──

    @staticmethod
    def key_run(tenant: str, run_id: str) -> str:
        return rkey(f"subagent:{tenant}:run:{run_id}")

    @staticmethod
    def key_events(tenant: str, run_id: str) -> str:
        return rkey(f"subagent:{tenant}:ev:{run_id}")

    @staticmethod
    def key_children(tenant: str, parent_run_id: str) -> str:
        return rkey(f"subagent:{tenant}:children:{parent_run_id}")

    @staticmethod
    def key_tree(tenant: str, root_session_id: str) -> str:
        return rkey(f"subagent:{tenant}:tree:{root_session_id}")

    # ── 写入端 ──

    async def start_run(
        self,
        *,
        run_id: str,
        tenant: str,
        root_session_id: str,
        parent_run_id: str = "",
        depth: int = 1,
        profile: str = "",
        task: str = "",
        status: str = "running",
    ) -> None:
        """登记 run：状态 Hash + 父节点索引 + 整树骨架。"""
        if not self.available:
            return
        summary = {"run_id": run_id, "status": status, "depth": depth,
                   "parent_run_id": parent_run_id, "profile": profile,
                   "task": (task or "")[:200]}
        try:
            pipe = self._redis.pipeline()
            pipe.hset(self.key_run(tenant, run_id), mapping={
                "run_id": run_id,
                "status": status,
                "depth": str(depth),
                "parent_run_id": parent_run_id,
                "profile": profile,
            })
            pipe.expire(self.key_run(tenant, run_id), self._ttl)
            # 整树骨架：一次 HGETALL 就能画出层级
            pipe.hset(self.key_tree(tenant, root_session_id), run_id, json.dumps(summary, ensure_ascii=False))
            pipe.expire(self.key_tree(tenant, root_session_id), self._ttl)
            # 子节点索引（顶层委派的父为根会话）
            parent_key = self.key_children(tenant, parent_run_id or root_session_id)
            pipe.sadd(parent_key, run_id)
            pipe.expire(parent_key, self._ttl)
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001 - 缓存失败不影响执行
            self._degrade("start_run", exc)

    async def push_event(self, *, run_id: str, tenant: str, payload: dict[str, Any]) -> None:
        """写入一条（已限流的）进度事件到 Stream。"""
        if not self.available:
            return
        try:
            key = self.key_events(tenant, run_id)
            await self._redis.xadd(
                key,
                {"data": json.dumps(payload, ensure_ascii=False)},
                maxlen=self._max_events,
                approximate=True,
            )
            await self._redis.expire(key, self._ttl)
        except Exception as exc:  # noqa: BLE001
            self._degrade("push_event", exc)

    async def publish_live_event(self, *, payload: dict[str, Any]) -> None:
        """把一条事件**实时**广播给网关（网关订阅后转投 SSE）。

        Stream（:meth:`push_event`）负责「回放」，pub/sub 负责「实时」—— 两者都要：
        前端只从网关的 `/events` 收推送；没有这条广播，后台子 Agent 的进度就只能靠
        「选中某个 run 时 3s 轮询」，既不实时、还要求用户先点开那个 run。
        """
        if not self.available:
            return
        try:
            await self._redis.publish(
                rkey("subagent:events"), json.dumps(payload, ensure_ascii=False)
            )
        except Exception as exc:  # noqa: BLE001 - 广播失败绝不能影响子 Agent
            self._degrade("publish_live_event", exc)

    async def update_status(
        self,
        *,
        run_id: str,
        tenant: str,
        status: str,
        summary: str = "",
        usage: dict[str, Any] | None = None,
        result_ref: str = "",
    ) -> None:
        """更新状态/摘要/用量（侧边栏与树直接读这些字段）。

        整树骨架里的摘要回写由 :meth:`update_tree_summary` 负责（它需要 root_session_id）。
        """
        if not self.available:
            return
        mapping: dict[str, Any] = {"status": status}
        if summary:
            mapping["summary"] = summary[:4000]
        if usage:
            mapping["usage"] = json.dumps(usage, ensure_ascii=False)
        if result_ref:
            mapping["result_ref"] = result_ref
        try:
            key = self.key_run(tenant, run_id)
            await self._redis.hset(key, mapping=mapping)
            await self._redis.expire(key, self._ttl)
        except Exception as exc:  # noqa: BLE001
            self._degrade("update_status", exc)

    async def update_tree_summary(
        self,
        *,
        tenant: str,
        root_session_id: str,
        run_id: str,
        summary: str,
        status: str,
        depth: int,
        parent_run_id: str = "",
        profile: str = "",
        usage: dict[str, Any] | None = None,
    ) -> None:
        """把摘要回写进整树骨架（避免前端为 N 个子节点发 N 次查询）。

        ``usage`` 必须一起回写：整树骨架正是 ``GET /v1/subagent/runs`` 的数据源，
        少了用量会让面板的用量列与合计恒为 0（只有详情接口才读 run Hash）。
        """
        if not self.available:
            return
        item = {
            "run_id": run_id, "status": status, "depth": depth,
            "parent_run_id": parent_run_id, "profile": profile,
            "summary": (summary or "")[:1000],
        }
        if usage:
            item["usage"] = usage
        try:
            key = self.key_tree(tenant, root_session_id)
            await self._redis.hset(key, run_id, json.dumps(item, ensure_ascii=False))
            await self._redis.expire(key, self._ttl)
        except Exception as exc:  # noqa: BLE001
            self._degrade("update_tree_summary", exc)

    # ── helpers ──

    def _degrade(self, where: str, exc: Exception) -> None:
        """Redis 不可用/命令不支持：记一次 warning 后本进程不再重试。"""
        message = str(exc)
        # Redis 是**非权威**面（权威状态在 PG），但它承载前端看到的进度与摘要 ——
        # 它坏掉时表现为"前端什么都没有"，必须能被计数，否则只能靠读日志发现。
        SUBAGENT_PERSIST_FAILED.labels(component="redis", op=where).inc()
        if not self._broken:
            logger.warning("subagent 运行期缓存不可用（%s）: %s —— 本次运行仅落 PG", where, message[:200])
        self._broken = True


async def get_runtime_cache() -> SubagentRuntimeCache | None:
    """取得（并懒初始化）运行期缓存；Redis 不可用时返回 None。"""
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
    except Exception as exc:  # noqa: BLE001
        logger.debug("subagent runtime cache 初始化失败: %s", str(exc)[:160])
        return None
    if redis is None:
        return None
    return SubagentRuntimeCache(redis)
