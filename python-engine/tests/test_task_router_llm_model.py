"""会话选的模型必须走到 LLM 能力执行（M53）。

背景：/chat 走统一链路时，`agent:*` 这几个 LLM 能力的 input_schema 只声明了
`task`，执行器拿不到会话运行时状态，于是 chat_stream 一路带 model="" 打上游。
DeepSeek 这类"只认名字不认前缀"的网关直接 400，界面上是一句
"LLM capability returned empty response"（现象：发消息后一直「思考中」）。

测试覆盖:
- AGENT 能力（参数表不含 model）注入会话 model / provider_hint
- 非 AGENT 能力不被污染
- 真·空模型不注入（不在这里发明默认值）
- 能力自己声明了 model 参数时不被会话值覆盖
- route_task 把 context.llm_config 透传到意图理解与执行两段
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.capabilities import (
    Capability,
    CapabilityParam,
    CapabilityResult,
    CapabilityType,
    WorkstationType,
)
from app.core.task_router import SubTask, TaskRouter


@pytest.fixture(autouse=True)
def _no_trace_writer(monkeypatch):
    """span 落 Redis Stream，与「模型有没有传到」无关 —— 隔离掉，别依赖 Redis。"""

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    # _execute_single_task 内部是 `from app.trace import record_span`（调用时才解析），
    # 所以打模块属性即可生效。
    monkeypatch.setattr("app.trace.record_span", _noop)


class _FakeRegistry:
    """只按 id 返回固定能力，避免污染全局注册表单例。"""

    def __init__(self, cap: Capability) -> None:
        self._cap = cap

    async def get_by_id(
        self, capability_id: str, tenant_id: int = 0
    ) -> Capability | None:
        return self._cap if capability_id == self._cap.capability_id else None


def _make_cap(
    workstation_type: WorkstationType,
    *,
    params: list[CapabilityParam] | None = None,
) -> tuple[Capability, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    async def _executor(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"reply": "ok"}

    cap = Capability(
        capability_id=f"{workstation_type.value}:probe",
        name="probe",
        description="test capability",
        workstation_type=workstation_type,
        capability_type=CapabilityType.SERVICE,
        input_schema=params
        if params is not None
        else [_task_param()],
        output_schema=CapabilityResult(fields=["reply"]),
        tags=["probe"],
        _executor=_executor,
    )
    return cap, seen


def _task_param() -> CapabilityParam:
    return CapabilityParam(
        name="task", type="string", description="任务描述", required=True
    )


async def _run(
    cap: Capability,
    *,
    parameters: dict[str, Any] | None = None,
    llm_model: str = "",
    provider_hint: str = "",
) -> dict[str, Any]:
    router = TaskRouter()
    router.registry = _FakeRegistry(cap)  # type: ignore[assignment]
    result = await router._execute_single_task(
        SubTask(
            subtask_id="sub_0",
            description="你好",
            capability_id=cap.capability_id,
            parameters=parameters
            if parameters is not None
            else {"task": "你好"},
        ),
        tenant_id=1,
        trace_id="",
        completed_tasks={},
        llm_model=llm_model,
        provider_hint=provider_hint,
    )
    assert result.status == "completed", f"执行失败: {result.error}"
    return result.input_params


# ── 注入条件 ────────────────────────────────────────────────────────


class TestModelInjection:
    async def test_agent_capability_receives_session_model(self):
        cap, seen = _make_cap(WorkstationType.AGENT)
        params = await _run(
            cap, llm_model="deepseek-flash", provider_hint="deepseek"
        )
        assert params["model"] == "deepseek-flash"
        assert params["provider_hint"] == "deepseek"
        # 执行器确实收到了同一份参数（不是只写进 input_params 记录）
        assert seen == [params]

    async def test_empty_session_model_is_not_invented(self):
        cap, _ = _make_cap(WorkstationType.AGENT)
        params = await _run(cap)
        assert "model" not in params
        assert "provider_hint" not in params

    async def test_non_agent_capability_not_polluted(self):
        cap, _ = _make_cap(WorkstationType.SKILL)
        params = await _run(
            cap, llm_model="deepseek-flash", provider_hint="deepseek"
        )
        assert "model" not in params
        assert "provider_hint" not in params

    async def test_capability_own_model_param_wins(self):
        """能力自己声明了 model 参数 → 归参数表管，会话值不覆盖。"""
        cap, _ = _make_cap(
            WorkstationType.AGENT,
            params=[
                _task_param(),
                CapabilityParam(
                    name="model",
                    type="string",
                    description="模型",
                    required=False,
                    default="cap-default",
                ),
            ],
        )
        params = await _run(
            cap,
            parameters={"task": "你好", "model": "cap-default"},
            llm_model="deepseek-flash",
            provider_hint="deepseek",
        )
        assert params["model"] == "cap-default"
        assert "provider_hint" not in params

    async def test_executor_none_still_reports_error(self):
        """守一下前提：能力没执行器时不该被当成「跑成功了」。"""
        cap, _ = _make_cap(WorkstationType.AGENT)
        cap._executor = None
        router = TaskRouter()
        router.registry = _FakeRegistry(cap)  # type: ignore[assignment]
        result = await router._execute_single_task(
            SubTask(
                subtask_id="sub_0",
                description="你好",
                capability_id=cap.capability_id,
                parameters={"task": "你好"},
            ),
            tenant_id=1,
            trace_id="",
            completed_tasks={},
            llm_model="deepseek-flash",
        )
        assert result.status == "failed"


class TestLLMExecutorLastMile:
    """执行器 → gateway.chat_stream 的最后一棒（断在这里时界面只看到
    "LLM capability returned empty response"，看不出是模型没传）。"""

    async def test_executor_forwards_model_and_hint_to_gateway(self, monkeypatch):
        from app.core.capabilities import _llm_executor

        seen: dict[str, Any] = {}

        class _Chunk:
            content = "你好，我是助手"

        class _FakeGateway:
            async def chat_stream(self, messages, model, **kwargs):  # noqa: ARG002
                seen["model"] = model
                seen["provider_hint"] = kwargs.get("provider_hint", "<缺失>")
                yield _Chunk()

        async def _get_gateway():
            return _FakeGateway()

        import app.main as app_main

        monkeypatch.setattr(app_main, "get_gateway", _get_gateway)

        executor = _llm_executor("system prompt")
        out = await executor(task="你好", model="deepseek-flash", provider_hint="deepseek")

        assert seen["model"] == "deepseek-flash"
        assert seen["provider_hint"] == "deepseek"
        assert out["result"] == "你好，我是助手"

    async def test_executor_without_model_stays_empty(self, monkeypatch):
        """没会话模型时不发明默认值 —— 由网关的兜底口径决定用谁。"""
        from app.core.capabilities import _llm_executor

        seen: dict[str, Any] = {}

        class _Chunk:
            content = "ok"

        class _FakeGateway:
            async def chat_stream(self, messages, model, **kwargs):  # noqa: ARG002
                seen["model"] = model
                seen["provider_hint"] = kwargs.get("provider_hint", "<缺失>")
                yield _Chunk()

        async def _get_gateway():
            return _FakeGateway()

        import app.main as app_main

        monkeypatch.setattr(app_main, "get_gateway", _get_gateway)

        await _llm_executor("system prompt")(task="你好")

        assert seen["model"] == ""
        assert seen["provider_hint"] == ""


# ── route_task 透传 ─────────────────────────────────────────────────


def _stub_route(
    router: TaskRouter,
    seen: dict[str, Any],
    subtasks: list[SubTask],
) -> None:
    async def _intent(user_input, context, **kwargs):
        seen["intent"] = kwargs
        return {"type": "general", "keywords": ["你好"], "entities": {}}

    async def _decompose(intent, tenant_id, user_input):
        return subtasks

    async def _match(tasks, tenant_id):
        return tasks

    async def _execute(execution_order, **kwargs):
        seen["execute"] = kwargs
        return []

    async def _aggregate(results, intent, user_input):
        return {"reply": "ok"}

    router._understand_intent = _intent  # type: ignore[method-assign]
    router._decompose_task = _decompose  # type: ignore[method-assign]
    router._match_capabilities = _match  # type: ignore[method-assign]
    router._execute_tasks = _execute  # type: ignore[method-assign]
    router._aggregate_results = _aggregate  # type: ignore[method-assign]


class TestRouteTaskForwarding:
    async def test_llm_config_reaches_execution(self):
        router = TaskRouter()
        seen: dict[str, Any] = {}
        _stub_route(
            router,
            seen,
            [
                SubTask(
                    subtask_id="sub_0",
                    description="你好",
                    capability_id="agent:general_chat",
                    parameters={"task": "你好"},
                )
            ],
        )

        result = await router.route_task(
            "你好",
            tenant_id=1,
            context={"llm_config": {"model": "deepseek-flash", "provider": "deepseek"}},
        )

        assert result["status"] == "completed"
        assert seen["intent"]["model"] == "deepseek-flash"
        assert seen["intent"]["provider_hint"] == "deepseek"
        assert seen["execute"]["llm_model"] == "deepseek-flash"
        assert seen["execute"]["provider_hint"] == "deepseek"

    async def test_missing_llm_config_forwards_empty(self):
        """没配会话模型 → 一路空串，交给网关兜底口径，不在这里发明默认值。"""
        router = TaskRouter()
        seen: dict[str, Any] = {}
        _stub_route(
            router,
            seen,
            [
                SubTask(
                    subtask_id="sub_0",
                    description="你好",
                    capability_id="agent:general_chat",
                    parameters={"task": "你好"},
                )
            ],
        )

        await router.route_task("你好", tenant_id=1, context={})

        assert seen["intent"]["model"] == ""
        assert seen["execute"]["llm_model"] == ""
        assert seen["execute"]["provider_hint"] == ""
