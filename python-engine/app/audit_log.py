"""审计时间戳的**唯一**实现 —— 三个 JSONL 审计模块共用。

为什么单独立一个模块：`exec_audit.jsonl`（执行）、`approval_audit.jsonl`（审批）、
`hooks_audit.jsonl`（钩子）三份流水会被**同一套收集/排障流程**合并读取（N4），
时间戳格式必须一致。此前三处各自写 `time.strftime("%Y-%m-%dT%H:%M:%S")` ——
重复即漂移（见 vendor/规划.md §4 的"两份同构的表"教训），所以收敛到一处。

**为什么不是本地时间**：`time.strftime` 给的是**本机本地时间**，且没有时区标记、
只有秒级精度。多副本部署里容器通常是 UTC、开发机是本地时区 —— 合并后的审计流既无法
排序（差一个时区偏移），也无法在跨副本排障时与其它证据对齐；同一秒内的多条记录还会
失去先后。这与路线图 L4-3 处理"naive 时间列"是**同一类**问题，只是发生在日志侧。

格式：`YYYY-MM-DDTHH:MM:SS.sssZ`（UTC、毫秒、显式 `Z`）。仍是 ISO-8601，仍是
`YYYY-MM-DDTHH:MM:SS` 前缀（既有 `jq .ts` / 字符串排序不受影响），但不再有歧义。
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_timestamp() -> str:
    """返回当前 UTC 时间戳：`YYYY-MM-DDTHH:MM:SS.sssZ`。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
