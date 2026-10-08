"""L4-1 端到端故障注入：**跑到第 2 回合 kill 进程 → 新实例续跑**。

这是设计 §7 批 3 的验收，此前一直是"手工判定"（见路线图 L4-1）。现有 38 条 checkpoint/resume
用例都是**单元级**：它们证明各零件正确，但**不证明跨进程**端到端。本用例补上这一条：

```text
子进程（真 AgentRuntime + 脚本化 provider）
  turn 1: LLM → tool_call(c1) → 工具执行 → 回合末 checkpoint 落盘
  turn 2: LLM → 卡住            ← 测试在这里 **硬杀**
断言：DB 里 status=running / checkpoint.turn_index=1 / done_tools=[c1]
新实例：
  load_resume_state → turn_index=1（⇒ 第 1 回合不重放）
  真再跑一次 runtime → **第一次 LLM 调用的历史里已经带着 c1 的工具结果**（⇒ 没重放）
  且 c1 的工具结果在历史里**恰好一份**（不重复执行）
```

需要真实 PostgreSQL（`integration` 标记；无 DSN 或连不上即 skip）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

import pytest

ENGINE_DIR = pathlib.Path(__file__).resolve().parents[1]
DRILL = ENGINE_DIR / "tests" / "drills" / "resume_kill_drill.py"


def _dsn() -> str:
    return os.environ.get("POSTGRES_DSN", "").strip()


async def _fetch_row(dsn: str, session_id: str) -> dict | None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            "SELECT status, checkpoint, checkpoint_at FROM agent_runs WHERE session_id = $1",
            session_id,
        )
        return dict(row) if row else None
    finally:
        await conn.close()


async def _wait_for_checkpoint(dsn: str, session_id: str, timeout: float = 45.0) -> dict:
    """轮询直到第 1 回合的 checkpoint 落盘（子进程此后会卡在第 2 回合）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = await _fetch_row(dsn, session_id)
        if row and row["checkpoint"]:
            payload = row["checkpoint"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if int(payload.get("turn_index") or 0) >= 1:
                return {**row, "checkpoint": payload}
        await asyncio.sleep(0.25)
    raise AssertionError("子进程 45s 内没有落下第 1 回合的 checkpoint")


async def _cleanup(dsn: str, session_id: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DELETE FROM agent_runs WHERE session_id = $1", session_id)
    finally:
        await conn.close()


def _messages_of(captured: dict) -> list[dict]:
    """取出 `chat_stream(messages=…)` 的历史消息并归一成 dict（元素可能是 pydantic 模型）。"""
    raw = captured.get("messages")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
            continue
        dump = getattr(item, "model_dump", None)
        out.append(dump() if callable(dump) else dict(getattr(item, "__dict__", {})))
    return out


@pytest.mark.integration
def test_kill_mid_run_then_new_instance_resumes_without_replay() -> None:
    dsn = _dsn()
    if not dsn:
        pytest.skip("需要 POSTGRES_DSN")

    session_id = f"drill-{uuid.uuid4().hex[:12]}"
    env = {
        **os.environ,
        "POSTGRES_DSN": dsn,
        "DRILL_SESSION_ID": session_id,
        "DRILL_TENANT_ID": "t1",
        # 子进程直接跑脚本文件时 sys.path[0] 是脚本目录 ⇒ 显式把引擎根放进 PYTHONPATH
        "PYTHONPATH": os.pathsep.join(
            [str(ENGINE_DIR), os.environ.get("PYTHONPATH", "") or ""]
        ).strip(os.pathsep),
    }
    proc = subprocess.Popen(
        [sys.executable, str(DRILL)],
        cwd=str(ENGINE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        row = asyncio.run(_wait_for_checkpoint(dsn, session_id))
    except AssertionError:
        proc.kill()
        out = proc.stdout.read()[-3000:] if proc.stdout else ""
        pytest.fail(f"演练子进程没有落下 checkpoint，输出：\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()  # **硬杀**：这是本用例的核心动作（进程无法自证死亡）
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)

    try:
        # ── ① 杀完之后，磁盘上的现场必须是"第 1 回合已完成" ──
        ckpt = row["checkpoint"]
        assert row["status"] == "running", f"被杀前状态应为 running，实际 {row['status']}"
        assert ckpt["turn_index"] == 1, f"应停在第 2 回合入口，实际 turn_index={ckpt['turn_index']}"
        assert ckpt["done_tools"] == ["c1"], f"已完成工具应记为 c1，实际 {ckpt['done_tools']}"
        tool_results = [m for m in ckpt["messages"] if m.get("role") == "tool"]
        assert [m.get("tool_call_id") for m in tool_results] == ["c1"], "快照里的工具结果形状不对"

        # ── ② 新实例读现场：应直接从第 2 回合继续（第 1 回合不重放）──
        # ③ 真跑一次续跑：第一次 LLM 调用的历史里必须已带 c1 的工具结果
        #
        # ②③ 必须在**同一个事件循环**里做：连接池绑定创建它的循环，跨 `asyncio.run` 复用
        # 会得到 "another operation is in progress"（实测踩到过）。
        from app.agent import resume as resume_mod

        captured: dict = {}

        async def _resume_path():
            from app.db import close_pool, init_pool
            from app.tools.context import set_tool_context

            await init_pool(dsn)
            set_tool_context(tenant_id="t1", user_id="u-drill", session_id=session_id)
            try:
                state = await resume_mod.load_resume_state(tenant_id="t1", session_id=session_id)

                from app.agent.runtime import AgentRuntime, AgentTask
                from app.gateway.provider import ChatResponse

                class _Gateway:
                    async def chat_stream(self, **kwargs: object):
                        captured.update(kwargs)
                        yield ChatResponse(content="resumed-done", finish_reason="stop")

                runtime = AgentRuntime(gateway=_Gateway())  # type: ignore[arg-type]
                task = AgentTask(
                    id="drill-task",
                    tenant_id="t1",
                    user_id="u-drill",
                    session_id=session_id,
                    content="drill",
                    system_prompt="sp",
                    llm_config={"mode": "normal", "tools_mode": "yolo"},
                    max_turns=5,
                )
                events = [event async for event in runtime.run(task)]
                return state, events
            finally:
                await close_pool()

        state, events = asyncio.run(_resume_path())
        assert state is not None, "新实例没有读到现场（续跑的前提就不成立）"
        assert state.turn_index == 1, f"续跑应从 turn_index=1 起，实际 {state.turn_index}"
        assert events, "续跑没有任何事件"

        messages = _messages_of(captured)
        dump = [
            (
                getattr(e, "type", "?"),
                str(getattr(e, "error", "") or "")[:120],
                str(getattr(e, "content", "") or "")[:60],
            )
            for e in events
        ]
        assert messages, (
            "续跑的第一次 LLM 调用没有带上历史消息（那等于没有续跑）；"
            f"captured keys={sorted(captured)}；events={dump}"
        )
        replay = [m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "c1"]
        assert len(replay) == 1, (
            f"第 1 回合的工具结果应恰好一份（不重放），实际 {len(replay)} 份；"
            f"历史角色序列={[m.get('role') for m in messages]}"
        )
        assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in messages), (
            "历史里应保留第 1 回合的 assistant(tool_calls) —— 否则续跑等于从头来"
        )
    finally:
        with contextlib.suppress(Exception):
            asyncio.run(_cleanup(dsn, session_id))
