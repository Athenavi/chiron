"""批 G：生命周期 hook 的注册表与执行编排（方案 03 §3）。

对外门面是模块级单例 `hooks`。runtime 只与**事件方法**交互
（`session_start` / `before_tool_use` / `after_tool_use` / `subagent_start` /
`subagent_stop` / `stop`），不直接接触注册表或沙箱。

三条硬约束（方案 03 §3.2）：
1. **默认关**：`settings.hooks_enabled=False` 时所有事件方法立即返回 —— 开启前零行为变化；
2. **只有 `PreToolUse` 可阻断**：其余事件 fire-and-forget，失败/超时/崩溃都不改主流程；
3. **多租户下用户无法注册 hook**：`owner` 区分部署级与租户级，用户自定义需
   `settings.hooks_allow_user_defined=True`（启用时由 `warn_if_user_defined_enabled`
   在启动处告警 + 审计 —— 不引入 `DEPLOYMENT_MODE`，因为判定部署形态不可靠）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.hooks import audit, events
from app.hooks.runner import HookOutcome, run_hook

logger = logging.getLogger(__name__)

#: 注册来源：部署级（部署者，受信） / 租户级（用户自定义，需显式开放）。
OWNER_DEPLOYMENT = "deployment"
OWNER_USER = "user"


@dataclass(frozen=True)
class Hook:
    """一个已注册的 hook 定义（进程内、重启生效 —— 注册属部署层能力）。"""

    event: str
    name: str
    code: str
    owner: str = OWNER_DEPLOYMENT


class HookRegistry:
    """按事件索引的 hook 注册表（进程内单例）。"""

    def __init__(self) -> None:
        self._by_event: dict[str, list[Hook]] = {}

    def add(self, hook: Hook) -> None:
        self._by_event.setdefault(hook.event, []).append(hook)

    def for_event(self, event: str) -> list[Hook]:
        return list(self._by_event.get(event, ()))

    def all(self) -> list[Hook]:
        return [item for hooks_of_event in self._by_event.values() for item in hooks_of_event]

    def clear(self) -> None:
        self._by_event.clear()


def _attr(obj: Any, name: str) -> str:
    """从 task（或任意携带身份的对象）安全取一个字符串字段。"""
    return str(getattr(obj, name, "") or "")


def _parse_arguments(tool_call: dict[Any, Any]) -> Any:
    """把工具参数解析成可 JSON 序列化的对象（解析失败时原样返回字符串）。"""
    raw = tool_call.get("arguments")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _outcome_label(outcome: HookOutcome) -> str:
    if outcome.timed_out:
        return "timeout"
    if not outcome.success:
        return "error"
    return "ok"


def _blocking_decision(outcome: HookOutcome) -> str | None:
    """从 PreToolUse 的执行结果里解析"是否阻断"，返回原因或 None。

    只有 hook **成功返回**且**显式 deny** 才算阻断；超时/崩溃/非法输出一律**放行** ——
    对接"超时按失败处理但不阻断主流程"（方案 03 §3.2）。这也保证了 PreToolUse 的
    失败是 fail-open 而非 fail-closed（否则 hook 一崩就把所有工具调用掐死）。
    """
    if not outcome.success:
        return None
    output = outcome.output
    if not isinstance(output, dict):
        return None
    denied = output.get("decision") in ("deny", "block") or output.get("block") is True
    if not denied:
        return None
    return str(output.get("reason") or output.get("message") or "denied by hook")


class HookManager:
    """hook 注册与事件编排门面（模块级单例 `hooks`）。"""

    def __init__(self) -> None:
        self.registry = HookRegistry()

    # ── 注册 ──────────────────────────────────────────────────────────

    def register(
        self,
        event: str,
        name: str,
        code: str,
        *,
        owner: str = OWNER_DEPLOYMENT,
    ) -> bool:
        """注册一个 hook，返回是否成功。

        用户自定义 hook（`owner=OWNER_USER`）在 `hooks_allow_user_defined=False`
        时被**拒绝**（多租户下用户无法注册 hook，方案 03 §3.2），并留一条审计 ——
        误开的 SaaS 部署要能从日志里看见"有人试图注册租户 hook"。

        部署级注册不受 `hooks_allow_user_defined` 限制，但**是否执行**仍受总开关
        `hooks_enabled` 约束（可先注册、后开启）。
        """
        if event not in events.ALL_EVENTS:
            logger.warning("reject hook %r: unknown event %r", name, event)
            return False
        if owner == OWNER_USER and not settings.hooks_allow_user_defined:
            audit.record_hook(
                event=event,
                hook=name,
                owner=OWNER_USER,
                outcome="register_denied",
                reason="user-defined hooks are disabled (hooks_allow_user_defined=false)",
            )
            logger.warning(
                "rejected user-defined hook %r on %s: hooks_allow_user_defined=false",
                name,
                event,
            )
            return False
        self.registry.add(Hook(event=event, name=name, code=code, owner=owner))
        return True

    def clear(self) -> None:
        self.registry.clear()

    # ── 事件执行 ──────────────────────────────────────────────────────

    async def _trigger(
        self, event: str, context: dict[str, Any]
    ) -> list[tuple[Hook, HookOutcome]]:
        """执行某事件的全部 hook，逐个落审计，返回 `(hook, outcome)` 列表。

        - 默认关时立即返回空列表（零行为变化）；
        - 单个 hook 的宿主侧异常在此被吞掉（记 `error` 审计），**绝不冒泡**到主流程。
        """
        if not settings.hooks_enabled:
            return []
        results: list[tuple[Hook, HookOutcome]] = []
        timeout = float(settings.hooks_timeout_seconds or 5)
        for hook in self.registry.for_event(event):
            try:
                outcome = await run_hook(
                    name=hook.name, code=hook.code, context=context, timeout=timeout
                )
            except Exception as exc:  # noqa: BLE001 — hook 失败不影响主流程
                logger.warning("hook %r raised host-side error: %s", hook.name, exc)
                outcome = HookOutcome(
                    success=False,
                    output=None,
                    error=str(exc),
                    exit_code=None,
                    duration_ms=0,
                    timed_out=False,
                )
            audit.record_hook(
                event=event,
                hook=hook.name,
                owner=hook.owner,
                tenant=str(context.get("tenant_id", "")),
                user=str(context.get("user_id", "")),
                session=str(context.get("session_id", "")),
                duration_ms=outcome.duration_ms,
                exit_code=outcome.exit_code,
                outcome=_outcome_label(outcome),
                reason=outcome.error if not outcome.success else None,
            )
            results.append((hook, outcome))
        return results

    def _tool_context(
        self, event: str, task: Any, tool_call: dict[Any, Any], result: Any = None
    ) -> dict[str, Any]:
        return events.build_context(
            event,
            session_id=_attr(task, "session_id"),
            tenant_id=_attr(task, "tenant_id"),
            user_id=_attr(task, "user_id"),
            tool_name=str(tool_call.get("name", "")),
            tool_arguments=_parse_arguments(tool_call),
            tool_result=result,
        )

    async def session_start(self, *, task: Any) -> None:
        """`SessionStart` —— fire-and-forget（一次 run 进入主循环之前）。"""
        if not settings.hooks_enabled:
            return
        await self._trigger(
            events.SESSION_START,
            events.build_context(
                events.SESSION_START,
                session_id=_attr(task, "session_id"),
                tenant_id=_attr(task, "tenant_id"),
                user_id=_attr(task, "user_id"),
            ),
        )

    async def stop(self, *, task: Any) -> None:
        """`Stop` —— fire-and-forget（一次 run 结束时，含异常/中断退出路径）。"""
        if not settings.hooks_enabled:
            return
        await self._trigger(
            events.STOP,
            events.build_context(
                events.STOP,
                session_id=_attr(task, "session_id"),
                tenant_id=_attr(task, "tenant_id"),
                user_id=_attr(task, "user_id"),
            ),
        )

    async def before_tool_use(self, *, task: Any, tool_call: dict[Any, Any]) -> str | None:
        """`PreToolUse` —— **唯一可阻断**主流程的事件。

        返回阻断原因字符串表示"拒绝执行该工具"；返回 `None` 表示放行。
        调用方（runtime）在工具策略/审批判定**之后**调用它，因此 hook 只能进一步
        收紧，不能放宽 —— 它没有"放行"语义。
        """
        if not settings.hooks_enabled:
            return None
        context = self._tool_context(events.PRE_TOOL_USE, task, tool_call)
        for hook, outcome in await self._trigger(events.PRE_TOOL_USE, context):
            reason = _blocking_decision(outcome)
            if reason:
                # 追加一条 blocked=True 的审计：区分"执行了但放行"与"执行了且拦下"。
                audit.record_hook(
                    event=events.PRE_TOOL_USE,
                    hook=hook.name,
                    owner=hook.owner,
                    tenant=str(context.get("tenant_id", "")),
                    user=str(context.get("user_id", "")),
                    session=str(context.get("session_id", "")),
                    duration_ms=outcome.duration_ms,
                    exit_code=outcome.exit_code,
                    blocked=True,
                    outcome="blocked",
                    reason=reason,
                )
                return f"blocked by PreToolUse hook '{hook.name}': {reason}"
        return None

    async def after_tool_use(self, *, task: Any, tool_call: dict[Any, Any], result: Any) -> None:
        """`PostToolUse` / `PostToolUseFailure` —— fire-and-forget，按结果成功/失败分派。"""
        if not settings.hooks_enabled:
            return
        failed = isinstance(result, dict) and bool(result.get("error"))
        event = events.POST_TOOL_USE_FAILURE if failed else events.POST_TOOL_USE
        await self._trigger(event, self._tool_context(event, task, tool_call, result))

    async def subagent_start(self, *, task: Any, tool_call: dict[Any, Any]) -> None:
        """`SubagentStart` —— fire-and-forget。"""
        if not settings.hooks_enabled:
            return
        await self._trigger(
            events.SUBAGENT_START, self._tool_context(events.SUBAGENT_START, task, tool_call)
        )

    async def subagent_stop(
        self, *, task: Any, tool_call: dict[Any, Any], result: Any = None
    ) -> None:
        """`SubagentStop` —— fire-and-forget。"""
        if not settings.hooks_enabled:
            return
        await self._trigger(
            events.SUBAGENT_STOP,
            self._tool_context(events.SUBAGENT_STOP, task, tool_call, result),
        )


#: 进程内单例 —— runtime 的唯一接触点。
hooks = HookManager()


def warn_if_user_defined_enabled() -> None:
    """启动告警：用户自定义 hook 被打开时给出**显著**日志 + 审计（方案 03 §3.2）。

    "SaaS 部署误开必须可见"是显式开关路线的可见性要求（既然排除了不可靠的
    部署形态判定，就靠这行告警兜底）。由 `app/main.py` 的启动流程调用 ——
    见交付说明的"需要主 agent 接线"。
    """
    if settings.hooks_enabled and settings.hooks_allow_user_defined:
        logger.warning(
            "SECURITY: user-defined lifecycle hooks are ENABLED "
            "(hooks_enabled=true & hooks_allow_user_defined=true) — tenant-provided code "
            "will run in a sandboxed subprocess beside the engine; keep this OFF on SaaS"
        )
        audit.record_hook(
            event="*",
            hook="<startup>",
            owner=OWNER_USER,
            outcome="user_hooks_enabled",
        )
