#!/usr/bin/env python
"""sandbox-service 并发压测 —— 「并发上限该定多少 / 该不该扩容」的实测口径。

## 为什么需要它

扩缩决策不能拍脑袋：sandbox-service 的单实例上限由 `SANDBOX_MAX_INFLIGHT`（每个
worker 的并发闸门）、容器的 `pids_limit` / `mem_limit`、以及宿主 CPU 共同决定。
本脚本用**并发阶梯**把这几条边界压出来。

## 用法

    # 直连单实例（本地调试）
    python scripts/bench_sandbox.py --url http://localhost:8001 --token "$INTERNAL_TOKEN"

    # 经 nginx 均衡打多副本（compose 场景）
    python scripts/bench_sandbox.py --url http://localhost:8001 \\
        --levels 1,2,4,8,16,32 --per-level 200

    # 压 CPU 密集命令（看饱和点），而不是 sleep（看调度/排队）
    python scripts/bench_sandbox.py --url http://localhost:8001 --command-kind cpu

## 怎么读结果

* **吞吐拐点**：并发继续涨但 RPS 不再涨 ⇒ 已到实例上限。
* **`rejected_total` 增长** ⇒ 排队超过 `SANDBOX_QUEUE_TIMEOUT_SECONDS`，需要扩容
  （或调大 `SANDBOX_MAX_INFLIGHT` / `pids_limit`）。
* **`queued` 持续 > 0 且无 rejected** ⇒ 容量刚好，突发会表现为延迟。
* **P99 陡增** ⇒ 同上。

把「RPS 不再涨且 P99 可接受」的并发数作为**每实例安全并发数**，据此设
`SANDBOX_SERVICE_MAX_CONCURRENCY`（引擎侧闸门）与副本数 M。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any

import httpx

RUN_PATH = "/v1/internal/exec/run"
STATS_PATH = "/v1/internal/exec/stats"

#: 两种负载形态：sleep 看调度/排队（不烧 CPU），cpu 看饱和点。
COMMANDS = {
    "sleep": "python -c \"import time; time.sleep(0.2)\"",
    "cpu": "python -c \"sum(i*i for i in range(3_000_000))\"",
}


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


async def _read_stats(client: httpx.AsyncClient, url: str, token: str) -> dict[str, Any]:
    try:
        resp = await client.get(f"{url}{STATS_PATH}", headers={"X-Internal-Token": token})
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — 老版本服务没有 /stats：如实说明而不是崩
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _run_level(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    *,
    concurrency: int,
    per_level: int,
    command: str,
    request_timeout: float,
) -> dict[str, Any]:
    """按固定并发跑 per_level 次请求，返回该级的时延与失败统计。"""
    latencies: list[float] = []
    failures = 0
    sem = asyncio.Semaphore(concurrency)

    async def one() -> None:
        nonlocal failures
        async with sem:
            t0 = time.perf_counter()
            try:
                resp = await client.post(
                    f"{url}{RUN_PATH}",
                    json={"command": command, "timeout_seconds": 60},
                    headers={"X-Internal-Token": token},
                    timeout=request_timeout,
                )
                resp.raise_for_status()
                body = resp.json()
                if body.get("exit_code") not in (0,):
                    failures += 1
            except Exception:  # noqa: BLE001 — 超时/连接失败都算失败
                failures += 1
            latencies.append((time.perf_counter() - t0) * 1000.0)

    started = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(per_level)))
    elapsed = time.perf_counter() - started

    return {
        "concurrency": concurrency,
        "requests": per_level,
        "elapsed_s": elapsed,
        "rps": per_level / elapsed if elapsed > 0 else 0.0,
        "failures": failures,
        "fail_rate": failures / per_level if per_level else 0.0,
        "p50_ms": _percentile(latencies, 50),
        "p95_ms": _percentile(latencies, 95),
        "p99_ms": _percentile(latencies, 99),
        "mean_ms": statistics.fmean(latencies) if latencies else 0.0,
    }


async def main_async(args: argparse.Namespace) -> int:
    url = args.url.rstrip("/")
    token = args.token or os.getenv("INTERNAL_TOKEN", "")
    if not token:
        print("错误：缺少令牌（--token 或 INTERNAL_TOKEN 环境变量）", file=sys.stderr)
        return 2

    command = args.command or COMMANDS[args.command_kind]
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    print(f"目标：{url}")
    print(f"负载：{args.command_kind}（{command}）")
    print(f"并发阶梯：{levels}，每级 {args.per_level} 次请求\n")

    async with httpx.AsyncClient() as client:
        before = await _read_stats(client, url, token)
        print("起始 /stats：", json.dumps(before, ensure_ascii=False))
        print()

        header = f"{'并发':>4} {'RPS':>8} {'失败率':>7} {'P50':>8} {'P95':>8} {'P99':>8}"
        print(header)
        print("-" * len(header))

        for level in levels:
            r = await _run_level(
                client,
                url,
                token,
                concurrency=level,
                per_level=args.per_level,
                command=command,
                request_timeout=args.timeout,
            )
            print(
                f"{level:>4} {r['rps']:>8.1f} {r['fail_rate'] * 100:>6.1f}% "
                f"{r['p50_ms']:>7.0f}ms {r['p95_ms']:>7.0f}ms {r['p99_ms']:>7.0f}ms"
            )

        print()
        after = await _read_stats(client, url, token)
        print("结束 /stats：", json.dumps(after, ensure_ascii=False))

    print(
        "\n判读：RPS 不再随并发上涨 ⇒ 已到实例上限；rejected_total 增长 ⇒ 需扩容"
        "（或调大 SANDBOX_MAX_INFLIGHT / pids_limit）。把「RPS 不再涨且 P99 可接受」的"
        "并发数作为每实例安全并发数。"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="sandbox-service 并发压测")
    parser.add_argument("--url", default="http://localhost:8001", help="沙箱服务地址（可指向 nginx）")
    parser.add_argument("--token", default="", help="X-Internal-Token（默认取 INTERNAL_TOKEN）")
    parser.add_argument("--levels", default="1,2,4,8,16,32", help="并发阶梯（逗号分隔）")
    parser.add_argument("--per-level", type=int, default=100, help="每级请求数")
    parser.add_argument("--command-kind", choices=sorted(COMMANDS), default="sleep")
    parser.add_argument("--command", default="", help="自定义命令（覆盖 --command-kind）")
    parser.add_argument("--timeout", type=float, default=120.0, help="单请求超时（秒）")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
