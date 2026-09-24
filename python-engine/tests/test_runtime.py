"""Tests for app.agent.runtime — message compression helpers.

Regression: _snip_tool_results used to wrap tool-result text in {"data": ...}
and re-serialize it, corrupting the JSON format of the message content sent
back to the LLM (double-escaped). It must truncate the plain text directly.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.agent.runtime import (
    MAX_MESSAGES,
    TOOL_RESULT_MAX_CHARS,
    TOOL_RESULT_TAIL,
    AgentRuntime,
    AgentTask,
    CompactionConfig,
    _compact_messages,
    _ensure_valid_tool_sequence,
    _prune_messages,
    _snip_tool_results,
    _truncate_text,
    _truncate_tool_result,
)
from app.gateway.provider import ChatResponse
from app.session_store import SessionStore


@pytest.fixture(autouse=True)
def _stub_tool_broker(monkeypatch):
    """模块级桩掉 Tool Broker 授权（见 app/tools/broker.py）。

    真实实现会 HTTP 到网关拿授权，而这里测的是栅栏 / 审批路径本身。不桩掉的话，
    **fail-closed** 语义 —— "拿不到服务端授权就拒绝执行危险工具" —— 会让所有涉及
    run_code / shell_exec 的用例失败。那是刻意的生产行为，不是缺陷。
    """

    async def _allow(*_args, **_kwargs):
        return {
            "allowed": True,
            "level": "delete",
            "requires_user_approval": True,
            "requires_second_check": True,
        }

    monkeypatch.setattr("app.tools.broker.authorize", _allow)


class TestTruncateText:
    def test_short_text_unchanged(self):
        text = "short"
        assert _truncate_text(text) == text

    def test_long_text_keeps_head_and_tail(self):
        # 长度随常量缩放，避免调参（如 8K→16K）导致测试静默失效
        text = "a" * 100 + "B" * TOOL_RESULT_MAX_CHARS + "c" * 100
        truncated = _truncate_text(text)
        assert len(truncated) <= TOOL_RESULT_MAX_CHARS
        assert truncated.startswith("a" * 100)
        assert truncated.endswith("c" * 100)
        assert "(truncated" in truncated

    def test_truncate_tool_result_reuses_text_logic(self):
        result = {"data": "x" * 5000}
        truncated = _truncate_tool_result(result)
        assert truncated.startswith('{"data": "xxx')  # keeps original JSON shape


class TestToolSequenceRepair:
    """回归：assistant(tool_calls) 部分配对（中断/审批等待）会触发 API 400，
    必须在恢复时从未配对 id 中移除缺失的 tool_calls。"""

    def _tc(self, cid: str, name: str = "read_file"):
        return {"id": cid, "name": name, "arguments": "{}"}

    def test_full_pairing_unchanged(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [self._tc("c1"), self._tc("c2")]},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        ]
        out = _ensure_valid_tool_sequence(msgs)
        assert out == msgs  # 完整配对原样保留

    def test_partial_pairing_drops_unmatched(self):
        """回归：assistant([c1,c2]) 只有 tool(c1)（c2 中断丢失）→ 移除 c2。"""
        msgs = [
            {"role": "assistant", "content": "调用工具", "tool_calls": [self._tc("c1"), self._tc("c2")]},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "user", "content": "继续"},
        ]
        out = _ensure_valid_tool_sequence(msgs)
        asst = next(m for m in out if m.get("role") == "assistant")
        assert asst["tool_calls"] == [self._tc("c1")]
        assert asst["content"] == "调用工具"  # 文本保留
        assert len(out) == 3  # user 消息保留

    def test_zero_pairing_clears_all(self):
        """assistant 带 tool_calls 但无任何 tool 结果 → 全部移除（保留 content）。"""
        msgs = [
            {"role": "assistant", "content": "我尝试调用但被中断", "tool_calls": [self._tc("c1")]},
            {"role": "user", "content": "继续"},
        ]
        out = _ensure_valid_tool_sequence(msgs)
        asst = next(m for m in out if m.get("role") == "assistant")
        assert asst.get("tool_calls") == []
        assert "我尝试调用但被中断" in asst["content"]

    def test_orphan_tool_dropped(self):
        """孤立 tool 消息（无前导 assistant）→ 删除（原行为保留）。"""
        msgs = [{"role": "tool", "tool_call_id": "c9", "content": "x"}]
        assert _ensure_valid_tool_sequence(msgs) == []


class TestCompactionConfig:
    """SaaS：截断策略可配置（strategy/阈值/大小由租户或模式确认）。"""

    def _long_tool_msgs(self, n: int = 12) -> list[dict]:
        """真实 OpenAI 交错格式：assistant(tool_calls) 紧跟 tool 结果。"""
        msgs: list[dict] = []
        for i in range(n):
            msgs.append({"role": "assistant", "content": None,
                         "tool_calls": [{"id": f"c{i}", "type": "function",
                                         "function": {"name": "read_file", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 2000})
        return msgs

    def test_strategy_none_keeps_all(self):
        msgs = self._long_tool_msgs(12)
        cfg = CompactionConfig(strategy="none", max_messages=4)
        assert _compact_messages(msgs, cfg) == msgs

    def test_strategy_prune_respects_max_messages(self):
        msgs = self._long_tool_msgs(12)
        cfg = CompactionConfig(strategy="prune", max_messages=6)
        out = _compact_messages(msgs, cfg)
        assert len(out) <= 6 + 1  # 系统消息数为 0，保留最近预算内 + 配对补齐

    def test_strategy_snipe_only_truncates_tool_results(self):
        msgs = self._long_tool_msgs(3)
        cfg = CompactionConfig(strategy="snipe", tool_result_max_chars=500, tool_result_head=100, tool_result_tail=50)
        out = _compact_messages(msgs, cfg)
        assert len(out) == len(msgs)  # 消息数不变
        for m in out:
            if m.get("role") == "tool":
                assert "(truncated" in m["content"]  # 长结果被截断

    def test_custom_tool_result_budget_applied(self):
        """自定义截断大小生效（deepseek retainTokens 语义的近似）。"""
        result = {"data": "x" * 3000}
        cfg = CompactionConfig(tool_result_max_chars=400, tool_result_head=100, tool_result_tail=50)
        truncated = _truncate_tool_result(result, cfg)
        assert "(truncated" in truncated

    def test_auto_uses_default_thresholds(self):
        """auto 策略默认行为与旧逻辑一致（阈值 0.8 prune / 0.6 snipe）。"""
        msgs = self._long_tool_msgs(3)
        out = _compact_messages(msgs)  # 无 cfg → 默认
        assert isinstance(out, list) and len(out) <= len(msgs)


class TestPruneMessages:
    def test_prune_keeps_recent_tool_result_content(self):
        # Regression: prune used to replace every tool result with the
        # placeholder "[tool_result:compressed]", so the agent lost all
        # tool-output context mid-task and got stuck.
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "x"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "data-" + "v" * 5000},
        ]
        for _ in range(3):  # push total over MAX_MESSAGES
            msgs += [
                {"role": "assistant", "content": "next", "tool_calls": [{"id": f"c{_}", "function": {"name": "x"}}]},
                {"role": "tool", "tool_call_id": f"c{_}", "content": "step-" + "w" * 5000},
            ]
        out = _prune_messages(msgs)
        joined = "".join(m.get("content", "") for m in out)
        # No placeholder anywhere
        assert "[tool_result:compressed]" not in joined
        # Recent tool result content survives (head + tail)
        assert joined.count("step-") >= 1
        # Pairing integrity: every tool msg is preceded by an assistant with tool_calls
        last_tc = False
        for m in out:
            if m.get("role") == "tool":
                assert last_tc, "orphan tool message after prune"
            last_tc = m.get("role") == "assistant" and bool(m.get("tool_calls"))
        # Budget respected — allow +1 for pairing-integrity protection
        # (never cut an assistant(tool_calls)↔tool pair, which would orphan
        # the tool message and lose the result entirely)
        assert len(out) <= MAX_MESSAGES + 1

    def test_prune_short_tool_result_kept_verbatim(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "x"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
        ]
        out = _prune_messages(msgs)
        assert any(m.get("content") == '{"ok": true}' for m in out)

    def test_prune_pairing_survives_interleaved_plain_assistant(self):
        # Regression: cutting inside a tool exchange can leave an orphan tool
        # msg preceded only by a plain assistant (no tool_calls); the tool result
        # then gets dropped before the LLM call.
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "x"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r1-" + "v" * 5000},
            {"role": "assistant", "content": "plain note"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "function": {"name": "y"}}]},
            {"role": "tool", "tool_call_id": "c2", "content": "r2-" + "w" * 5000},
        ]
        for _ in range(3):
            msgs += [
                {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{_}", "function": {"name": "z"}}]},
                {"role": "tool", "tool_call_id": f"c{_}", "content": "later-" + "q" * 5000},
            ]
        out = _prune_messages(msgs)
        last_tc = False
        for m in out:
            if m.get("role") == "tool":
                assert last_tc, "orphan tool message after prune (interleaved case)"
            last_tc = m.get("role") == "assistant" and bool(m.get("tool_calls"))


class TestSnipToolResults:
    def test_short_tool_content_unchanged(self):
        content = '{"result": "ok"}'
        messages = [{"role": "tool", "tool_call_id": "call_1", "content": content}]
        out = _snip_tool_results(messages)
        assert out[0]["content"] == content

    def test_long_tool_content_is_truncated_not_wrapped(self):
        # Regression: previously became {"data": "{\"result\": ...}"}
        long_json = '{"result": "' + "x" * 5000 + '"}'
        messages = [{"role": "tool", "tool_call_id": "call_1", "content": long_json}]
        out = _snip_tool_results(messages)
        content = out[0]["content"]
        # Must stay plain JSON text of the tool result — not re-wrapped in {"data": ...}
        assert not content.startswith('{"data":')
        # Truncated text must still end with the original tail
        assert content.endswith(long_json[-TOOL_RESULT_TAIL:])

    def test_other_roles_untouched(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        out = _snip_tool_results(messages)
        assert out == messages

    def test_snipped_content_is_still_valid_json_when_within_budget(self):
        # Content slightly over the snip threshold but under the hard max stays intact JSON
        content = '{"result": "' + "y" * (TOOL_RESULT_MAX_CHARS // 2 + 50) + '"}'
        messages = [{"role": "tool", "tool_call_id": "c", "content": content}]
        out = _snip_tool_results(messages)
        assert json.loads(out[0]["content"])["result"] == "y" * (TOOL_RESULT_MAX_CHARS // 2 + 50)


class TestRuntimeModes:
    """模式配置在 runtime 中的落地：工具集过滤 + persona 覆盖 + 压缩开关。"""

    @staticmethod
    def _registered_names() -> set[str]:
        import app.tools.core  # noqa: F401 — 注册真实核心工具
        import app.tools.mode_admin  # noqa: F401 — 注册创作工具（创造模式）
        import app.tools.run_code  # noqa: F401 — 注册 run_code
        from app.tools.registry import registry
        return set(registry.list_names())

    async def _run_and_capture(self, mode: str, system_prompt: str = "默认提示词"):
        assert "read_file" in self._registered_names(), "真实工具未注册"
        calls: list[dict] = []
        gw = MagicMock()

        async def fake_stream(**kwargs):
            calls.append(kwargs)
            yield ChatResponse(content="ok", finish_reason="stop")

        gw.chat_stream = fake_stream
        runtime = AgentRuntime(gateway=gw)
        task = AgentTask(id="t1", tenant_id="t", user_id="u", session_id="",
                         content="hi", system_prompt=system_prompt,
                         llm_config={"mode": mode}, max_turns=2)
        events = [e async for e in runtime.run(task)]
        return calls, events

    @pytest.mark.asyncio
    async def test_normal_mode_exposes_full_core_tools_no_run_code(self):
        calls, events = await self._run_and_capture("normal")
        names = {t["function"]["name"] for t in calls[0]["tools"]}
        assert "read_file" in names and "shell_exec" in names
        assert "run_code" not in names  # extra 工具不暴露
        assert events[-1].type == "done"

    @pytest.mark.asyncio
    async def test_minimal_mode_exposes_only_its_core_tools(self):
        calls, events = await self._run_and_capture("minimal", system_prompt="外部默认提示词")
        names = {t["function"]["name"] for t in calls[0]["tools"]}
        # 极简模式只给"读 / 改 / 跑"三个，**外加 tool_search** —— 它是按需激活的入口，
        # 必须始终可见，否则模型根本不知道还能搜索更多工具（见 runtime._get_core_tools）。
        assert names == {"read_file", "edit_file", "shell_exec", "tool_search"}
        # persona 覆盖：system 消息是固定 persona，而非外部传入的默认提示词
        system_msgs = [m for m in calls[0]["messages"] if m.role == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0].content == "You are a helpful software engineer assistant."

    @pytest.mark.asyncio
    async def test_ptc_mode_exposes_run_code(self):
        calls, events = await self._run_and_capture("ptc")
        names = {t["function"]["name"] for t in calls[0]["tools"]}
        assert "run_code" in names
        assert "read_file" in names

    @pytest.mark.asyncio
    async def test_creative_mode_exposes_authoring_tools(self):
        calls, events = await self._run_and_capture("creative")
        names = {t["function"]["name"] for t in calls[0]["tools"]}
        assert "mode_list" in names and "mode_edit" in names

    @pytest.mark.asyncio
    async def test_unknown_mode_falls_back_to_normal(self):
        calls, events = await self._run_and_capture("bogus")
        names = {t["function"]["name"] for t in calls[0]["tools"]}
        assert "run_code" not in names
        assert "read_file" in names


class TestContextContinuity:
    """S 修复：上下文丢失 — 首轮中断（SSE 停止）后，第二轮必须从缓存恢复历史。"""

    async def _interrupted_first_turn(self, session_id: str):
        """第一轮：输出 content 后模拟 SSE 中断（async generator 被 aclose）。"""
        from unittest.mock import MagicMock
        gw = MagicMock()
        first_call = True

        async def fake_stream(**kwargs):
            nonlocal first_call
            if first_call:
                first_call = False
                yield ChatResponse(content="我准备写", finish_reason="stop")
            else:
                yield ChatResponse(content="继续中", finish_reason="stop")

        gw.chat_stream = fake_stream
        store = SessionStore(max_sessions=20)
        runtime = AgentRuntime(gateway=gw, session_store=store)
        task1 = AgentTask(id="t1", tenant_id="t", user_id="u", session_id=session_id,
                          content="写一段排序代码", max_turns=2)
        # 只消费第一个事件后立即中断（模拟前端停止/断流）
        gen = runtime.run(task1)
        await gen.__anext__()
        await gen.aclose()  # GeneratorExit → finally 保存缓存
        return runtime

    @pytest.mark.asyncio
    async def test_second_turn_resumes_history_after_interrupt(self):
        """中断后第二轮能看到第一轮的用户消息（缓存 finally 保存生效）。"""
        from unittest.mock import MagicMock
        runtime = await self._interrupted_first_turn("sess-cont-1")
        gw2 = MagicMock()
        captured_messages: list[list[dict]] = []

        async def fake_stream2(**kwargs):
            captured_messages.append(kwargs.get("messages", []))
            yield ChatResponse(content="第二轮回合", finish_reason="stop")

        runtime._gateway = gw2  # 复用同一 runtime（同一缓存路径）
        gw2.chat_stream = fake_stream2
        task2 = AgentTask(id="t2", tenant_id="t", user_id="u", session_id="sess-cont-1",
                          content="继续", max_turns=2)
        _ = [e async for e in runtime.run(task2)]
        # 第二轮请求的 messages 必须包含第一轮的用户消息（上下文未丢）
        assert captured_messages, "第二轮未产生 LLM 请求"
        msgs = captured_messages[0]
        texts = [m.content for m in msgs if m.role == "user"]
        assert any("写一段排序代码" in t for t in texts), f"第一轮用户消息丢失: {texts}"


class TestToolGuardConfirmPath:
    """回归：确认工具必须先 yield approval 事件（前端卡片）再等待用户决定。

    曾经的实现把 ``await`` 放在 ``_guarded_execute_tool`` 内部——事件要等批准
    后才返回，前端永远收不到确认卡片，任务挂起 300 秒超时。
    """

    @staticmethod
    def _tc(cid: str):
        return {"id": cid, "name": "run_code", "arguments": "{}"}

    @pytest.mark.asyncio
    async def test_confirm_emits_event_before_waiting(self):

        runtime = AgentRuntime(gateway=None)
        task = AgentTask(id="t1", tenant_id="t", user_id="u", session_id="",
                         content="hi", max_turns=2)
        # run_code 属于 DANGEROUS_TOOLS → confirm：立即返回 (None, approval_evt)
        tool_result, evt = await runtime._guarded_execute_tool(self._tc("tc1"), task)
        assert tool_result is None, "confirm 分支不得阻塞等待，必须立即返回事件"
        assert evt is not None and evt.type == "approval"
        assert evt.tool_call_id == "tc1"
        assert "tc1" in runtime._pending_approvals, "approval future 未注册"

    @pytest.mark.asyncio
    async def test_approve_resumes_execution(self):
        import asyncio as _asyncio

        runtime = AgentRuntime(gateway=None)
        task = AgentTask(id="t1", tenant_id="t", user_id="u", session_id="",
                         content="hi", max_turns=2)
        _tool_result, evt = await runtime._guarded_execute_tool(self._tc("tc1"), task)
        assert evt is not None
        # 前端收到事件后用户批准 → _await_approval 恢复并执行工具
        fut = _asyncio.ensure_future(runtime._await_approval(self._tc("tc1"), task))
        await _asyncio.sleep(0.05)
        solved = await runtime.submit_approval("tc1", True)
        assert solved is True
        result = await _asyncio.wait_for(fut, timeout=5.0)
        assert isinstance(result, dict), "批准后应返回工具执行结果（dict）"

    @pytest.mark.asyncio
    async def test_denied_returns_error(self):
        import asyncio as _asyncio

        runtime = AgentRuntime(gateway=None)
        task = AgentTask(id="t1", tenant_id="t", user_id="u", session_id="",
                         content="hi", max_turns=2)
        _tool_result, evt = await runtime._guarded_execute_tool(self._tc("tc2"), task)
        assert evt is not None
        fut = _asyncio.ensure_future(runtime._await_approval(self._tc("tc2"), task))
        await _asyncio.sleep(0.05)
        solved = await runtime.submit_approval("tc2", False)
        assert solved is True
        result = await _asyncio.wait_for(fut, timeout=5.0)
        assert "denied" in result.get("error", "")
        assert "tc2" not in runtime._pending_approvals, "拒绝后应清理 pending 状态"


class TestTruncatedToolArguments:
    """坑2 回归：max_tokens 截断会让 tool_call 的 arguments 变成半截 JSON。

    拿着残缺参数执行工具可能写坏文件，因此必须拒绝执行、把错误回灌给模型，
    而不是用空参数蒙混过关（空参数还会绕过按参数匹配的栅栏规则）。
    """

    @staticmethod
    def _tc(cid: str, name: str, arguments: str):
        return {"id": cid, "name": name, "arguments": arguments}

    @pytest.mark.asyncio
    async def test_truncated_arguments_are_refused(self):
        runtime = AgentRuntime(gateway=None)
        task = AgentTask(id="t1", tenant_id="t", user_id="u", session_id="",
                         content="hi", max_turns=2)
        # 被 max_tokens 截断的半截 JSON：缺闭合引号与花括号
        truncated = '{"path": "a.txt", "content": "hel'
        tool_result, evt = await runtime._guarded_execute_tool(
            self._tc("tc1", "write_file", truncated), task
        )
        assert evt is None, "参数非法时不得进入 approval 流程"
        assert tool_result is not None
        assert "not valid JSON" in tool_result.get("error", "")
        assert "tc1" not in runtime._pending_approvals

    @pytest.mark.asyncio
    async def test_valid_arguments_are_not_refused(self):
        runtime = AgentRuntime(gateway=None)
        task = AgentTask(id="t1", tenant_id="t", user_id="u", session_id="",
                         content="hi", max_turns=2)
        # run_code 属 DANGEROUS_TOOLS（confirm）：合法参数应在参数校验之后进入
        # approval 流程（返回事件、不执行），既证明合法 JSON 未被误拒，也无副作用。
        tool_result, evt = await runtime._guarded_execute_tool(
            self._tc("tc2", "run_code", '{"code": "print(1)"}'), task
        )
        assert tool_result is None
        assert evt is not None and evt.type == "approval"


