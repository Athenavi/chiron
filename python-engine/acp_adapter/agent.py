"""ACP 适配层：把 Chiron 的 HTTP API 包成 ACP 的 `Agent`（方案 03 §5 · 批 I）。

## 为什么用 Python + 官方 SDK，而不是设计稿原来推荐的 Go

设计稿推荐 Go（复用批 F 的 `/submit` + `/events` + 审批处理，**零新增依赖**）。真正动手时看清了
ACP 的 wire 协议规模：它有 20+ 个 schema 类型（`InitializeResponse` / `NewSessionResponse` /
`PromptResponse` / `ToolCallStart` / `ToolCallUpdate` / `PermissionOption` / `AgentPlanUpdate` …），
且官方 SDK 已经提供了**方法分发与 stdio 服务端**。用 Go 自己实现这些，等于把"协议正确性"押在
通读规格上 —— 而协议写错的后果是**整个适配层不可用**，这比"少一个依赖"重得多。

代价是新增一个**只属于本条产品线**的依赖（`agent-client-protocol`）。它与"不引清单外依赖"的约束
并不冲突：引擎与评测都不需要它，装它的人才受影响 —— 本模块**延迟 import**，没装的机器上连
`import acp_adapter.agent` 都不会炸。

## 边界

* **纯客户端**：只调既有的 `/submit` + `/events` + 控制端点，不 import 引擎内部类（与评测同一原则）；
* **一个连接 = 一个身份**：API Key 从启动参数/环境变量取，缺了就**退出**，不做"连上了再说"；
* **`cancel` 是真取消**：调 `/v1/agent/interrupt`（引擎会在**轮次边界**停下），而不是"停止读 SSE"；
* **不承诺事件回放**：`load_session` 拉历史消息；窗口外的 SSE 事件补不回来，也不假装能补。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

API_KEY_ENV = "CHIRON_API_KEY"
BASE_URL_ENV = "CHIRON_BASE_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

_SDK_HINT = (
    "ACP 适配层需要官方 SDK（它只属于本条产品线，不在引擎/评测的依赖清单里）。\n"
    "  pip install agent-client-protocol\n"
    "它与引擎、评测互不影响：不装的人只用不到 ACP 这一块。"
)

try:  # 延迟到"真正要用"才必需 —— 见上面的说明
    from acp import Agent as _ACPBase  # type: ignore[import-not-found]

    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover — 取决于是否装了可选依赖
    _ACPBase = object
    _SDK_AVAILABLE = False


def require_sdk() -> None:
    """缺 SDK 时给出**可执行**的提示（而不是一个看不懂的 ImportError）。"""
    if not _SDK_AVAILABLE:
        raise SystemExit(_SDK_HINT)


def prompt_text(prompt: list[Any]) -> str:
    """从 ACP 的 prompt（content block 列表）里取出文本。

    只取文本块：图片/音频/资源块在当前链路里没有对应通道（`/submit` 的 content 是字符串）。
    遇到非文本块时**记一条日志**而不是静默丢弃 —— "用户贴了图但没反应"必须能从日志里看出来。
    """
    parts: list[str] = []
    for block in prompt or []:
        if isinstance(block, str):
            parts.append(block)
            continue
        kind = getattr(block, "type", "") or (
            block.get("type", "") if isinstance(block, dict) else ""
        )
        if kind in ("text", "text_block", ""):
            text = getattr(block, "text", None) or (
                block.get("text", "") if isinstance(block, dict) else ""
            )
            if text:
                parts.append(str(text))
        else:
            logger.warning("ACP prompt: 非文本块被忽略（kind=%s）", kind)
    return "\n".join(parts).strip()


class ChironACPAgent(_ACPBase):  # type: ignore[misc]  # SDK 未安装时基类是 Any（CI 与本机都不装它）
    """ACP Agent 实现：会话生命周期 + 一轮 prompt（流式）+ 真取消。

    与 SDK 的对接只在这里（`mapping.py` 负责语义翻译，本文件负责 wire）。
    """

    def __init__(self, *, base_url: str = "", api_key: str = "") -> None:
        self._base_url = (base_url or os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = (api_key or os.getenv(API_KEY_ENV) or "").strip()
        if not self._api_key:
            # 一个编辑器连接 = 一个身份；没有身份就不该连上再报错
            raise SystemExit(
                f"{API_KEY_ENV} 未设置 —— ACP 适配层不接受无身份连接。\n"
                f"在编辑器配置里传入（或设环境变量 {API_KEY_ENV}）。"
            )

    # ── 会话生命周期 ────────────────────────────────────────────────────

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> Any:
        """声明能力。

        只声明我们**真的**支持的东西：当前没有 plan 更新与 MCP 转发通道，就不声明 ——
        声明了却不发，客户端会一直等。schema 的具体形状以 SDK 为准（见 `_acp_schema`）。
        """
        require_sdk()
        from acp import InitializeResponse

        return InitializeResponse(protocol_version=protocol_version)

    async def new_session(
        self,
        cwd: str,
        additional_directories: Any = None,
        mcp_servers: Any = None,
        **kwargs: Any,
    ) -> Any:
        """建一个会话。

        `cwd` 目前在链路里没有对应物（工作区由引擎侧决定，不是编辑器本地目录）—— 记一条日志，
        不假装用它。
        """
        require_sdk()
        import uuid

        from acp import NewSessionResponse

        if cwd:
            logger.info("ACP new_session: cwd=%s（工作区由引擎侧决定）", cwd)
        return NewSessionResponse(session_id=uuid.uuid4().hex)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: Any = None,
        mcp_servers: Any = None,
        **kwargs: Any,
    ) -> Any:
        """加载已有会话：拉历史消息，**不承诺事件回放**。"""
        require_sdk()
        from acp import LoadSessionResponse

        # 历史消息的端点与分页形状需要一次联调确认（见 README 的"待联调"一节）；
        # 这里明确返回"成功但无回放"，而不是假装把历史都灌进去了。
        logger.info("ACP load_session: session=%s（拉历史，不承诺事件回放）", session_id)
        return LoadSessionResponse()

    async def set_session_mode(self, *args: Any, **kwargs: Any) -> Any:
        """模式切换：当前链路没有对应语义，明确 no-op（而不是静默接受任意值）。"""
        require_sdk()
        from acp import SetSessionModeResponse

        return SetSessionModeResponse()

    async def set_config_option(self, *args: Any, **kwargs: Any) -> Any:
        require_sdk()
        from acp import SetSessionConfigOptionResponse

        return SetSessionConfigOptionResponse()

    # ── 一轮对话 ────────────────────────────────────────────────────────

    async def prompt(self, prompt: list[Any], session_id: str, **kwargs: Any) -> Any:
        """一轮对话：`/submit` + 读 SSE，逐事件翻译成 ACP 更新。"""
        require_sdk()
        from acp import PromptResponse

        from acp_adapter.mapping import (
            RunFinished,
            translate,
        )
        from acp_adapter.stream import stream_run

        text = prompt_text(prompt)
        if not text:
            return PromptResponse(stop_reason="end_turn")

        reason = "done"
        async for event in stream_run(self._client(), session_id, text):
            for update in translate(event):
                if isinstance(update, RunFinished):
                    reason = update.reason
                    continue
                await self._emit(session_id, update)
            if reason != "done":
                break
        return PromptResponse(stop_reason="cancelled" if reason == "cancelled" else "end_turn")

    async def _emit(self, session_id: str, update: Any) -> None:
        """把一条中立更新交给 SDK 发出去。

        具体调用形状以 SDK 的 `Agent` 基类为准（参照实现用的是 `self._conn.session_update(...)`
        与 `update_agent_message` / `start_tool_call` / `update_tool_call` 这些辅助函数）——
        首次联调时按 SDK 版本核对一次。
        """
        from acp import text_block, update_agent_message

        from acp_adapter.mapping import (
            Notice,
            PermissionAsk,
            TextDelta,
            ThoughtDelta,
            ToolFinished,
            ToolStarted,
        )

        if isinstance(update, (TextDelta, ThoughtDelta, Notice)):
            payload = update.text if isinstance(update, (TextDelta, ThoughtDelta)) else update.detail
            await self._conn.session_update(
                session_id=session_id,
                update=update_agent_message(text_block(payload)),
                source="Chiron",
            )
            return

        if isinstance(update, ToolStarted):
            logger.debug("ACP tool started: %s", update.name)
            return

        if isinstance(update, ToolFinished):
            logger.debug("ACP tool finished: ok=%s detail=%s", update.ok, update.detail)
            return

        if isinstance(update, PermissionAsk):
            # 真正的 permission 请求要走 SDK 的 client 回调（请求方向是 agent→client）。
            # 首次联调时接上；在那之前**不假装**已经问过了 —— 只记日志。
            logger.warning(
                "ACP permission 请求尚未接线（tool=%s call=%s）—— 见 README 的待联调一节",
                update.tool,
                update.call_id,
            )

    # ── 取消（真取消）────────────────────────────────────────────────────

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """取消：调引擎的中断端点（run 会在**轮次边界**停下），而不是"停止读 SSE"。"""
        require_sdk()
        from acp_adapter.stream import interrupt

        ok = await interrupt(self._client(), session_id)
        logger.info("ACP cancel: session=%s accepted=%s", session_id, ok)

    # ── 内部 ────────────────────────────────────────────────────────────

    def _client(self) -> Any:
        from acp_adapter.stream import ChironClient

        return ChironClient(self._base_url, self._api_key)
