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
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.hooks import audit, events, inject
from app.hooks.runner import (
    COMMAND_BLOCKED_PREFIX,
    HookOutcome,
    command_policy_error,
    run_command_hook,
    run_hook,
    run_webhook_hook,
    webhook_target_error,
)

logger = logging.getLogger(__name__)

#: 注册来源：部署级（部署者，受信） / 租户级（用户自定义，需显式开放）。
OWNER_DEPLOYMENT = "deployment"
OWNER_USER = "user"

#: 执行形态：`python`（既有：`plugin_runner.py` 沙箱子进程）·
#: `command`（批次 2b：**运维声明**的本地命令，独立开关默认关）·
#: `webhook`（批次 3：**出站 POST** 到运维声明的 URL，独立开关 + host 白名单，默认关）。
HANDLER_PYTHON = "python"
HANDLER_COMMAND = "command"
HANDLER_WEBHOOK = "webhook"

#: 已知的执行形态（注册时校验；`events.ALL_EVENTS` 之于事件名的同款作用）。
HANDLERS: frozenset[str] = frozenset({HANDLER_PYTHON, HANDLER_COMMAND, HANDLER_WEBHOOK})


@dataclass(frozen=True)
class Hook:
    """一个已注册的 hook 定义（进程内、重启生效 —— 注册属部署层能力）。"""

    event: str
    name: str
    code: str = ""
    owner: str = OWNER_DEPLOYMENT
    #: 匹配模式（空 = 该事件的所有调用都跑）。**只对工具类事件合法**（`events.MATCHER_EVENTS`）。
    #: 语义是**锚定**匹配（`re.fullmatch`）—— 未锚定会让"只想匹配 `file`"的钩子
    #: 意外命中 `read_file`（Reasonix 2.x 文档里的同款教训）。
    matcher: str = ""
    #: `matcher` 的编译结果（注册时编译一次；非法正则在注册处即被拒绝，不会拖到执行期）。
    matcher_re: re.Pattern[str] | None = field(default=None, repr=False, compare=False)
    #: 执行形态（`python` / `command` / `webhook`）。`command` 用 `command`、`webhook` 用 `url`。
    handler: str = HANDLER_PYTHON
    #: `command` 形态的命令文本（其余形态为空 —— python 的脚本定义在 `code` 里）。
    command: str = ""
    #: `webhook` 形态的目标 URL（其余形态为空）。
    url: str = ""

    @property
    def target(self) -> str:
        """审计里要记的"**运维声明的目标摘要**"（命令文本或 URL）；无目标时为空串。"""
        return self.command or self.url


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

    `command` 形态无需第二套判定：`run_command_hook` 已把**退出码 2** 折成
    `{"decision": "deny", ...}`，于是两种形态在这里合流（`docs/hook-protocol-design.md` §4.4）。
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


def _record_command_exec_audit(context: dict[str, Any], hook: Hook, outcome: HookOutcome) -> None:
    """`command` 形态**额外**落一条 exec 审计（`tool="hook_command"`）。

    为什么两处都记：`hooks_audit.jsonl` 是 hook 自己的流水（事件 + 判定 + 阻断），
    `exec_audit.jsonl` 是"**每条起进程的路径都留痕**"那一族，且**只有它会跨副本集中
    摄取**（N4）—— 运维声明的本地命令是本批次新增的可执行面，出事时必须能在集中审计里
    查到，而不是只留在某个副本的本地目录里。

    身份**显式传入**：`SessionStart` / `UserPromptSubmit` 触发点早于 runtime 的
    `set_tool_context`，那时 contextvars 还是空的（`exec_audit` 默认从那里取）。
    """
    from app.tools.exec_audit import (
        OUTCOME_BLOCKED,
        OUTCOME_ERROR,
        OUTCOME_OK,
        OUTCOME_TIMEOUT,
        record_execution,
    )

    if outcome.timed_out:
        outcome_label = OUTCOME_TIMEOUT
    elif str(outcome.error or "").startswith(COMMAND_BLOCKED_PREFIX):
        outcome_label = OUTCOME_BLOCKED
    elif not outcome.success:
        outcome_label = OUTCOME_ERROR
    else:
        outcome_label = OUTCOME_OK
    record_execution(
        tool="hook_command",
        command=hook.command,
        outcome=outcome_label,
        exit_code=outcome.exit_code,
        reason=outcome.error,
        duration_ms=outcome.duration_ms,
        tenant=str(context.get("tenant_id", "")),
        user=str(context.get("user_id", "")),
        session=str(context.get("session_id", "")),
    )


class HookManager:
    """hook 注册与事件编排门面（模块级单例 `hooks`）。"""

    def __init__(self) -> None:
        self.registry = HookRegistry()

    # ── 注册 ──────────────────────────────────────────────────────────

    def register(
        self,
        event: str,
        name: str,
        code: str = "",
        *,
        owner: str = OWNER_DEPLOYMENT,
        matcher: str = "",
        handler: str = HANDLER_PYTHON,
        command: str = "",
        url: str = "",
    ) -> bool:
        """注册一个 hook，返回是否成功。

        用户自定义 hook（`owner=OWNER_USER`）在 `hooks_allow_user_defined=False`
        时被**拒绝**（多租户下用户无法注册 hook，方案 03 §3.2），并留一条审计 ——
        误开的 SaaS 部署要能从日志里看见"有人试图注册租户 hook"。

        部署级注册不受 `hooks_allow_user_defined` 限制，但**是否执行**仍受总开关
        `hooks_enabled` 约束（可先注册、后开启）。

        `matcher`（批 G+ 批次 1）：**锚定**正则，只对工具类事件合法；非法正则或
        用错事件一律**当场拒绝并留审计** —— 配了却不生效是最难查的一类问题。

        `handler`（批 G+ 批次 2b/3）：`python`（默认）/ `command` / `webhook`。后两者是
        **运维面**的能力，因此注册处就设闸：独立的开关默认关 + 目标必须在各自的 allowlist 内
        （fail-closed），不满足一律拒绝并留审计。
        """
        if event not in events.ALL_EVENTS:
            logger.warning("reject hook %r: unknown event %r", name, event)
            return False
        if handler not in HANDLERS:
            audit.record_hook(
                event=event,
                hook=name,
                owner=owner,
                outcome="register_denied",
                reason=f"unknown handler type {handler!r}",
            )
            logger.warning("reject hook %r on %s: unknown handler type %r", name, event, handler)
            return False
        policy = None
        if handler == HANDLER_COMMAND:
            policy = command_policy_error(command)
        elif handler == HANDLER_WEBHOOK:
            policy = webhook_target_error(url)
        if policy:
            audit.record_hook(
                event=event,
                hook=name,
                owner=owner,
                outcome="register_denied",
                reason=policy,
            )
            logger.warning("reject %s hook %r on %s: %s", handler, name, event, policy)
            return False
        compiled: re.Pattern[str] | None = None
        if matcher:
            if not events.matcher_applies(event):
                audit.record_hook(
                    event=event,
                    hook=name,
                    owner=owner,
                    outcome="register_denied",
                    reason="matcher is only valid on tool events "
                    f"({', '.join(sorted(events.MATCHER_EVENTS))})",
                )
                logger.warning(
                    "reject hook %r on %s: matcher is only valid on tool events", name, event
                )
                return False
            try:
                compiled = re.compile(matcher)
            except re.error as exc:
                audit.record_hook(
                    event=event,
                    hook=name,
                    owner=owner,
                    outcome="register_denied",
                    reason=f"invalid matcher regex: {exc}",
                )
                logger.warning("reject hook %r on %s: invalid matcher regex: %s", name, event, exc)
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
        self.registry.add(
            Hook(
                event=event,
                name=name,
                code=code,
                owner=owner,
                matcher=matcher,
                matcher_re=compiled,
                handler=handler,
                command=command,
                url=url,
            )
        )
        return True

    def clear(self) -> None:
        self.registry.clear()

    # ── 事件执行 ──────────────────────────────────────────────────────

    async def _trigger(
        self, event: str, context: dict[str, Any], *, task: Any = None
    ) -> list[tuple[Hook, HookOutcome]]:
        """执行某事件中 **matcher 命中**的 hook，逐个落审计，返回 `(hook, outcome)` 列表。

        `task`（批次 4）：传入时把各 hook 输出的 `additional_context` **收集**到
        `task.hook_contexts`；注入本身由 runtime 在 system 段定形时完成（`app/hooks/inject.py`）。
        `None` = 不收集（例如没有 task 的调用方）。

        - 默认关时立即返回空列表（零行为变化）；
        - 无 `matcher` 的 hook 一律执行；有 `matcher` 的按**锚定**匹配工具名（见 `Hook.matcher`）；
        - 按 `Hook.handler` **分派形态**：`python` 走 `plugin_runner` 沙箱，`command` 走
          运维声明的本地命令（并**额外**落一条 `exec_audit`，见 `_record_command_exec_audit`），
          `webhook` 走**出站 POST**（不起进程，故不写 exec 审计 —— 那不是"执行路径"）；
        - **一个事件的累计预算**（`hooks_event_budget_seconds`）封顶总耗时：没有它时
          最坏情况是 `N × 单 hook 超时` 按 hook 数线性拖慢每个工具调用。预算耗尽后
          后续 hook **不执行**，但会留一条 `budget_exhausted` 审计（不静默跳过）；
          单个 hook 的常见情形行为不变（预算 > 单 hook 超时）；
        - 单个 hook 的宿主侧异常在此被吞掉（记 `error` 审计），**绝不冒泡**到主流程。
        """
        if not settings.hooks_enabled:
            return []
        results: list[tuple[Hook, HookOutcome]] = []
        timeout = float(settings.hooks_timeout_seconds or 5)
        budget = float(settings.hooks_event_budget_seconds or 10)
        deadline = time.monotonic() + budget
        subject = str(context.get(events.MATCHER_SUBJECT_FIELD, ""))
        for hook in self.registry.for_event(event):
            if hook.matcher_re is not None and hook.matcher_re.fullmatch(subject) is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                audit.record_hook(
                    event=event,
                    hook=hook.name,
                    owner=hook.owner,
                    tenant=str(context.get("tenant_id", "")),
                    user=str(context.get("user_id", "")),
                    session=str(context.get("session_id", "")),
                    outcome="budget_exhausted",
                    reason=f"event budget {budget:g}s exhausted; hook not run",
                )
                logger.warning(
                    "hook %r on %s not run: event budget (%.1fs) exhausted",
                    hook.name,
                    event,
                    budget,
                )
                break
            try:
                if hook.handler == HANDLER_COMMAND:
                    outcome = await run_command_hook(
                        name=hook.name,
                        command=hook.command,
                        context=context,
                        timeout=min(timeout, remaining),
                    )
                elif hook.handler == HANDLER_WEBHOOK:
                    outcome = await run_webhook_hook(
                        name=hook.name,
                        url=hook.url,
                        context=context,
                        timeout=min(timeout, remaining),
                    )
                else:
                    outcome = await run_hook(
                        name=hook.name,
                        code=hook.code,
                        context=context,
                        timeout=min(timeout, remaining),
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
                handler=hook.handler,
                # 运维声明的**目标摘要**（`command` 形态是命令文本、`webhook` 形态是 URL）。
                command=hook.target or None,
                truncated=outcome.truncated,
            )
            if hook.handler == HANDLER_COMMAND:
                _record_command_exec_audit(context, hook, outcome)
            results.append((hook, outcome))
        if task is not None:
            inject.collect(task, results)
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
            task=task,
        )

    async def user_prompt_submit(self, *, task: Any) -> None:
        """`UserPromptSubmit` —— fire-and-forget（收到用户提示词、输入护栏之后）。

        **刻意不可阻断**（不在 `BLOCKING_EVENTS`）：阻断用户自己的提示词是对**用户**
        的控制点，与 `PreToolUse`"只收紧工具策略"性质不同；拒绝输入已由输入护栏承担。
        见 `docs/hook-protocol-design.md` §4.1。
        """
        if not settings.hooks_enabled:
            return
        await self._trigger(
            events.USER_PROMPT_SUBMIT,
            events.build_context(
                events.USER_PROMPT_SUBMIT,
                session_id=_attr(task, "session_id"),
                tenant_id=_attr(task, "tenant_id"),
                user_id=_attr(task, "user_id"),
            ),
            task=task,
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
            task=task,
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
        for hook, outcome in await self._trigger(events.PRE_TOOL_USE, context, task=task):
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
                    handler=hook.handler,
                    command=hook.target or None,
                    truncated=outcome.truncated,
                )
                return f"blocked by PreToolUse hook '{hook.name}': {reason}"
        return None

    async def after_tool_use(self, *, task: Any, tool_call: dict[Any, Any], result: Any) -> None:
        """`PostToolUse` / `PostToolUseFailure` —— fire-and-forget，按结果成功/失败分派。"""
        if not settings.hooks_enabled:
            return
        failed = isinstance(result, dict) and bool(result.get("error"))
        event = events.POST_TOOL_USE_FAILURE if failed else events.POST_TOOL_USE
        await self._trigger(event, self._tool_context(event, task, tool_call, result), task=task)

    async def subagent_start(self, *, task: Any, tool_call: dict[Any, Any]) -> None:
        """`SubagentStart` —— fire-and-forget。"""
        if not settings.hooks_enabled:
            return
        await self._trigger(
            events.SUBAGENT_START,
            self._tool_context(events.SUBAGENT_START, task, tool_call),
            task=task,
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
            task=task,
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
