"""L4-1 kill→重启 演练：**被杀进程侧**（由 `tests/test_resume_kill_drill.py` 拉起后杀掉）。

它只做一件事：用真实 `AgentRuntime` 跑一个真任务 ——
第 1 回合发一个工具调用（回合末会落 checkpoint），第 2 回合**卡住等被杀**。

```text
turn 1: LLM → tool_call(c1) → 工具执行 → **回合末 checkpoint 落盘** → 继续
turn 2: LLM → 卡住（asyncio.sleep 3600）  ← 测试在这里 kill -9
```

因此杀掉时磁盘上必然有"第 1 回合已完成"的现场，正是续跑要接住的状态。
"""

import asyncio
import os
import sys


async def main() -> None:
    dsn = os.environ["POSTGRES_DSN"]
    session_id = os.environ["DRILL_SESSION_ID"]
    tenant_id = os.environ.get("DRILL_TENANT_ID", "t1")

    from app.db import init_pool
    from app.tools.context import set_tool_context

    await init_pool(dsn)
    set_tool_context(tenant_id=tenant_id, user_id="u-drill", session_id=session_id)

    from app.agent.runtime import AgentRuntime, AgentTask
    from app.gateway.provider import ChatResponse, ToolCall

    first = True

    class _Gateway:
        async def chat_stream(self, **_kwargs: object):
            nonlocal first
            if first:
                first = False
                yield ChatResponse(
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="read_file",
                            arguments='{"path": "drill-missing.txt"}',
                        )
                    ],
                )
            else:
                # 第 2 回合：**卡住**（此时第 1 回合的 checkpoint 已落盘，等测试来杀）
                await asyncio.sleep(3600)
                yield ChatResponse(content="never", finish_reason="stop")

    runtime = AgentRuntime(gateway=_Gateway())  # type: ignore[arg-type]
    task = AgentTask(
        id="drill-task",
        tenant_id=tenant_id,
        user_id="u-drill",
        session_id=session_id,
        content="drill",
        system_prompt="sp",
        llm_config={"mode": "normal", "tools_mode": "yolo"},
        max_turns=5,
    )
    async for _event in runtime.run(task):
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - 被 kill 时的正常路径
        sys.exit(0)
