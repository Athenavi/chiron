"""提交实现（E2）：真实 HTTP 路径 + 确定性替身。

- `HttpSubmit`：走网关 `POST /submit` + `GET /events`（SSE）—— **真实用户路径**。
  不 import 引擎内部类，因此测的是用户实际能用的东西，顺带覆盖网关 / SSE / 租户链路。
- `ScriptedSubmit`：**确定性替身** —— 按任务 id 返回脚本化事件流并落脚本声明的产物。
  零密钥、零费用，供 PR 门禁与 runner 单测使用（这也是"smoke 能作 PR 门禁"的前提）。

> `HttpSubmit` 的事件收集逻辑与 `ScriptedSubmit` 共用 `observation_from_events`，
> 所以"事件 → 观测"这条链路在替身下也被完整覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.assertions import Observation
from evals.firmware import Task
from evals.observe import observation_from_events

#: 视为"本次运行结束"的事件类型
TERMINAL_EVENTS = frozenset({"done", "error"})


class HttpSubmit:
    """走网关的提交实现。

    Args:
        base_url: 网关地址（如 `http://127.0.0.1:8080`）。
        api_key: 网关 API Key（`X-API-Key`，见 `internal/api/middleware.go`）。
        timeout: 单条任务的整体超时（秒）。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 300.0,
        session_prefix: str = "eval",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._session_prefix = session_prefix

    async def __call__(self, task: Task, workspace: Path) -> Observation:
        session_id = f"{self._session_prefix}-{task.id}"
        events = await self.run_session(session_id, task.prompt)
        return observation_from_events(events)

    async def run_session(self, session_id: str, prompt: str) -> list[dict[str, Any]]:
        """提交一条消息并收集到终态事件为止。

        `httpx` 在**函数内**导入：只有真实 HTTP 路径需要它，替身路径（`ScriptedSubmit`）
        因此保持**零外部依赖** —— 这正是 PR 门禁 job 能秒级完成的原因。
        """
        import httpx

        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        events: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout, headers=headers
        ) as client:
            await client.post("/submit", json={"session_id": session_id, "content": prompt})
            async with client.stream(
                "GET", "/events", params={"session_id": session_id}
            ) as resp:
                async for line in resp.aiter_lines():
                    event = _parse_sse_line(line)
                    if event is None:
                        continue
                    events.append(event)
                    if str(event.get("type", "")) in TERMINAL_EVENTS:
                        break
        return events


class ScriptedSubmit:
    """确定性替身：脚本里声明"发什么事件、落什么文件"。

    脚本形状（供 PR 门禁与单测）：

    ```python
    {
      "file-edit-single-value": {
        "events": [
          {"type": "tool_call", "tool_call_id": "c1", "tool_name": "write_file"},
          {"type": "tool_result", "tool_call_id": "c1", "content": "{}"},
          {"type": "done", "input_tokens": 10, "output_tokens": 5}
        ],
        "write_files": {"config.ini": "[app]\\ndebug=true\\n"}
      }
    }
    ```
    """

    def __init__(self, scripts: dict[str, dict[str, Any]]) -> None:
        self._scripts = scripts

    async def __call__(self, task: Task, workspace: Path) -> Observation:
        script = self._scripts.get(task.id) or {}
        for rel, content in (script.get("write_files") or {}).items():
            target = workspace / str(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
        return observation_from_events(list(script.get("events") or []))


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """解析一行 SSE；非 `data:` 行或非法 JSON 返回 None。

    网关的 SSE 形如 `data: {"type":"text",...}`（见 `internal/broadcast/hub.go` 的
    `FormatSSE`）；这里刻意**宽容**：解析不了的行跳过而不是抛错，避免一行噪音让整条任务失败。
    """
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
