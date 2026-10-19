"""队列规格 —— stream / 消费组 / DLQ / 延迟重试键。

**为什么需要这一层。** ``kb_index`` 的*执行者*与其它任务不同：它需要 PageIndex SDK，
而引擎镜像不装它（S4：索引由独立的 pageindex-worker 消费，见 docker-compose 的
``pageindex-worker`` 服务）。而 Redis Stream 是**单消费组**语义 —— 同一组内任意
consumer 都能领到任意消息，所以"让引擎 worker 别碰 kb_index"**不能靠过滤**（它已经
把消息从队列里拿走了），只能换一条 stream。

DLQ 与延迟重试键也**按队列分开**：否则 kb 任务的重试会落回引擎 worker 手里，
变成"怎么投都修不好"的死信。

滚动升级兼容：旧版 producer 可能仍把 ``kb_index`` 投到主 stream（它还不知道新队列）。
引擎 worker 遇到这种"不属于自己"的任务时**转发到目标队列并 ACK**（见 worker 的
``_dispatch``），而不是失败进 DLQ —— 否则一次滚动升级就会把期间的索引任务全打进死信。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.redis_keys import rkey


@dataclass(frozen=True)
class QueueSpec:
    """一条队列的完整键集合。``key`` 只用于日志/指标标签（不含环境前缀）。"""

    key: str
    stream: str
    group: str
    dlq: str
    delayed: str
    maxlen: int = 100000
    dlq_maxlen: int = 10000


#: 引擎任务（对话/记忆/工作流/工具作业…）—— 由 python-engine 内的 worker 消费。
ENGINE_QUEUE = QueueSpec(
    key="engine",
    stream=rkey("engine:tasks"),
    group="engine-workers",
    dlq=rkey("engine:tasks:dlq"),
    delayed=rkey("engine:tasks:delayed"),
)

#: 知识库索引 —— 由 pageindex-worker 消费（它装着 PageIndex SDK；引擎镜像不装）。
KB_QUEUE = QueueSpec(
    key="kb",
    stream=rkey("engine:tasks:kb"),
    group="kb-workers",
    dlq=rkey("engine:tasks:kb:dlq"),
    delayed=rkey("engine:tasks:kb:delayed"),
)

#: task_type → 队列。**新增"需要独立依赖/独立执行者"的任务类型时，在这里登记。**
#: 未登记的类型一律走引擎队列（保持既有行为）。
TASK_QUEUE: dict[str, QueueSpec] = {
    "kb_index": KB_QUEUE,
}

ALL_QUEUES: tuple[QueueSpec, ...] = (ENGINE_QUEUE, KB_QUEUE)

#: 兼容旧引用（这两个名字历史上指"主队列"）。新代码请直接用 ``QueueSpec`` ——
#: "任务该投哪条队列"只应该有一个答案（``queue_for_task``）。
TASK_STREAM = ENGINE_QUEUE.stream
DLQ_STREAM = ENGINE_QUEUE.dlq
GROUP_NAME = ENGINE_QUEUE.group


def queue_for_task(task_type: str) -> QueueSpec:
    """该任务类型该投到哪条队列。"""
    return TASK_QUEUE.get(task_type, ENGINE_QUEUE)


def is_foreign(task_type: str, current: QueueSpec) -> bool:
    """任务是否属于**别的**队列（滚动升级期间出现在本队列里 = 需要转发）。"""
    return queue_for_task(task_type) is not current
