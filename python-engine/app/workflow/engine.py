"""Workflow engine（Python 端）：DAG 执行引擎。

目标：
- 实现 Go 侧 `internal/graph/executor.go` 的等价 Python 执行能力
- 支持从 StateGraph JSON 编译并运行 workflow
- 节点类型：input / llm / tool / condition / output

执行方式：按拓扑序顺序调用各节点函数（手工维护 state），
不依赖 LangGraph 的图执行（其 StateGraph(dict) 为整 state 替换语义，
并行分支会产生 update 冲突）。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.gateway.router import GatewayRouter
from app.tools.registry import registry as tool_registry

logger = logging.getLogger(__name__)

NodeFn = Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]]


@dataclass
class NodeResult:
    node_id: str
    status: str  # completed / error / skipped
    output: str = ""
    error: str = ""
    duration_ms: int = 0


@dataclass
class WorkflowInstance:
    instance_id: str
    graph_name: str
    user_id: str = ""  # P1-1: 用户 ID 用于持久化
    state: dict[str, Any] = field(default_factory=dict)
    results: dict[str, NodeResult] = field(default_factory=dict)
    status: str = "running"
    error: str = ""  # P1-1: 错误信息
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


_instances: dict[str, WorkflowInstance] = {}
_MAX_INSTANCES = 500  # 最大实例数，防止内存泄漏
_instance_order: list[str] = []  # FIFO 顺序


def _topological_sort(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 DAG 拓扑顺序排序节点"""
    in_degree: Counter[str] = Counter({n["id"]: 0 for n in nodes})
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adj[e["source_id"]].append(e["target_id"])
        in_degree[e["target_id"]] += 1

    node_map = {n["id"]: n for n in nodes}
    queue = deque([n for n in nodes if in_degree.get(n["id"], 0) == 0])
    result: list[dict[str, Any]] = []
    while queue:
        node = queue.popleft()
        result.append(node)
        for neighbor in adj[node["id"]]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0 and neighbor in node_map:
                queue.append(node_map[neighbor])
    # 添加未在 edges 中出现的孤立节点
    seen = {n["id"] for n in result}
    for n in nodes:
        if n["id"] not in seen:
            result.append(n)
    return result


def _eval_condition(expression: str, text: str) -> bool:
    if not expression:
        return bool(text)
    return expression.lower() in text.lower()


def _build_node_fns(graph_json: dict[str, Any], gateway: GatewayRouter) -> dict[str, NodeFn]:
    """构建 node_id → 节点执行函数 的映射（不依赖 LangGraph）。

    每个节点函数签名 (state, node_id) -> 增量 dict（只含自身输出 key
    ``__out_<node_id>__``），由 run_workflow 按拓扑序调用并合并进 state。
    """
    nodes = graph_json.get("nodes", [])
    edges = graph_json.get("edges", [])
    node_map = {n["id"]: n for n in nodes}

    # 入边前驱映射（节点输入从前驱输出解析，而非共享的 __output__）
    preds: dict[str, list[str]] = {}
    for e in edges:
        preds.setdefault(e["target_id"], []).append(e["source_id"])

    def _prev_output(state: dict[str, Any], node_id: str) -> str:
        """取入边前驱节点的输出（最后一个前驱优先），无前驱时返回空串。"""
        for pid in reversed(preds.get(node_id, [])):
            v = state.get(f"__out_{pid}__")
            if v is not None:
                return str(v)
        return ""

    async def _input_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        node = node_map[node_id]
        if node_id in state:
            out = str(state[node_id])
        elif "input" in state:
            out = str(state["input"])
        else:
            out = f"[input] {node.get('label', node_id)}"
        # 只返回自身增量 key，由 run_workflow 合并进共享 state
        return {f"__out_{node_id}__": out}

    async def _llm_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        node = node_map[node_id]
        config = node.get("config", {})
        # 字段对齐（前端表单）：system_prompt + user_message；兼容旧 prompt
        system = config.get("system_prompt", "")
        user_msg = config.get("user_message", "")
        prompt = user_msg or config.get("prompt") or _prev_output(state, node_id)
        model = config.get("model", "")

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        text = ""
        try:
            async for chunk in gateway.chat_stream(
                messages=messages, model=model or "gpt-4o-mini"
            ):
                if chunk.content:
                    text += chunk.content
        except Exception as e:
            # 不能把错误文本当成"正常输出"返回 —— 那会让工作流以 status=completed 收尾，
            # 调用方看到的是"成功 + 一段 'llm error: ...' 文本"。节点失败必须上抛，
            # 由 run_workflow 落到 instance.status="error" / instance.error。
            logger.warning("LLM node %s failed: %s", node_id, e)
            raise
        return {f"__out_{node_id}__": text}

    async def _tool_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        node = node_map[node_id]
        config = node.get("config", {})
        name = config.get("tool_name", node.get("label", ""))
        params = config.get("params", {})
        # 节点级重试：失败（error 结果或异常）时按 config.retries 重试
        retries = int(config.get("retries", 0) or 0)
        last_output: Any = ""
        for attempt in range(retries + 1):
            try:
                result = await tool_registry.execute(name, params)
            except Exception as e:  # noqa: BLE001 — 工具异常按失败处理重试
                last_output = f"error: {e}"
                if attempt < retries:
                    continue
                break
            if isinstance(result, dict) and result.get("error"):
                last_output = str(result)
                if attempt < retries:
                    continue
                break
            last_output = str(result)
            break
        return {f"__out_{node_id}__": last_output}

    async def _condition_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        node = node_map[node_id]
        config = node.get("config", {})
        # 字段对齐（前端表单）：condition + variable；兼容旧 expression/input
        expr = config.get("condition") or config.get("expression", "")
        ref = config.get("variable") or config.get("input", "")
        text = _prev_output(state, node_id)
        if isinstance(ref, str) and ref.startswith("$"):
            key = ref[1:]
            # 兼容节点输出（__out_<id>__）与 state 同名 key 两种引用
            text = str(state.get(f"__out_{key}__", state.get(key, text)))
        matched = _eval_condition(expr, text)
        return {f"__out_{node_id}__": "true" if matched else "false"}

    async def _output_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        return {f"__out_{node_id}__": _prev_output(state, node_id)}

    async def _skill_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        """技能节点：调用已安装技能（复用 skill_run，支持 prompt/python/shell/http）。"""
        from app.tools.skill import skill_run

        node = node_map[node_id]
        config = node.get("config", {})
        skill_name = config.get("skill_name", "")
        if not skill_name:
            return {
                f"__out_{node_id}__": "error: skill_name is required in node config"
            }
        params = config.get("params", {}) or {}
        # 支持将前驱输出作为 skill 的 input 参数
        if (
            "input" in config
            and isinstance(config["input"], str)
            and config["input"].startswith("$")
        ):
            key = config["input"][1:]
            params = {**params, key: _prev_output(state, node_id)}
        result = await skill_run(skill_name, params)
        return {f"__out_{node_id}__": result.get("output", result.get("error", ""))}

    async def _knowledge_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        """知识库节点：检索知识库并把片段注入上下文（复用 kb_search）。"""
        from app.tools.kb import kb_search

        node = node_map[node_id]
        config = node.get("config", {})
        kb_id = config.get("kb_id", "")
        if not kb_id:
            return {f"__out_{node_id}__": "error: kb_id is required in node config"}
        query = config.get("query", "") or _prev_output(state, node_id)
        top_k = int(config.get("top_k", 5) or 5)
        result = await kb_search(kb_id, query, top_k=top_k)
        return {f"__out_{node_id}__": str(result)}

    async def _agent_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        """Agent 节点：调用已安装 Agent（任务取 config.task 或前置输出，配置可覆盖
        system_prompt/model/max_turns；租户取自执行上下文）。"""
        node = node_map[node_id]
        config = node.get("config", {})
        task = config.get("task", "") or _prev_output(state, node_id)
        if not task:
            return {f"__out_{node_id}__": "error: task is required"}
        from app.agent.multi_agent import SubAgent
        from app.tools.context import get_tenant_id

        try:
            agent = SubAgent(
                name=config.get("name", "workflow_agent"),
                description=config.get("description", "工作流 Agent 节点"),
                system_prompt=config.get("system_prompt", "")
                or "你是工作流中的智能体，完成指定任务并给出清晰、准确的结果。",
                gateway=gateway,
                model=config.get("model", "gpt-4o-mini"),
                max_turns=int(config.get("max_turns", 6) or 6),
            )
            result = await agent.run(
                task=str(task), tenant_id=get_tenant_id() or "default"
            )
        except Exception as e:  # noqa: BLE001
            return {f"__out_{node_id}__": f"agent error: {e}"}
        if not result.success:
            return {f"__out_{node_id}__": f"agent error: {result.error or 'unknown'}"}
        return {f"__out_{node_id}__": str(result.output)}

    node_fn = {
        "input": _input_node,
        "llm": _llm_node,
        "tool": _tool_node,
        "condition": _condition_node,
        "output": _output_node,
        "skill": _skill_node,
        "knowledge": _knowledge_node,
        "agent": _agent_node,
    }
    node_fns: dict[str, NodeFn] = {}
    for node in nodes:
        node_id = node["id"]
        base_fn = node_fn.get(node["node_type"], _input_node)

        async def _wrapped(
            state: dict[str, Any], _node_id: str, _base_fn: NodeFn = base_fn
        ) -> dict[str, Any]:
            result: dict[str, Any] = await _base_fn(state, _node_id)
            return result

        node_fns[node_id] = _wrapped

    return node_fns


async def run_workflow(
    graph_json: dict[str, Any],
    gateway: GatewayRouter,
    initial_state: dict[str, Any] | None = None,
    instance_id: str | None = None,
    resume_state: dict[str, Any] | None = None,
    resume_done: set[str] | None = None,
    on_node_done: Callable[[dict[str, Any], list[str]], Awaitable[None]] | None = None,
) -> WorkflowInstance:
    """执行 Workflow（DAG 按拓扑序）。

    断点续跑（多实例/崩溃恢复）：
    - resume_state / resume_done：从 DB checkpoint 恢复的累积 state 与已完成节点集合，
      主循环跳过已完成节点、从断点继续（节点为纯函数，state 含 __out_<id>__ 可重放）；
    - on_node_done(state, done_nodes)：每完成一个节点回调一次，供调用方持久化 checkpoint
      （如写 workflow_instances.checkpoint）；回调失败仅影响恢复粒度，不阻断执行。
    """
    instance = WorkflowInstance(
        instance_id=instance_id or f"wf_{uuid.uuid4().hex[:10]}",
        graph_name=graph_json.get("name", "workflow"),
        state=dict(resume_state if resume_state is not None else (initial_state or {})),
    )
    # 兜底设置工具会话上下文（幂等：不覆盖调用方已设置的 user/tenant）
    from app.tools.context import set_tool_context

    set_tool_context(session_id=instance.instance_id)

    _instances[instance.instance_id] = instance
    # FIFO 淘汰：超过最大数量时删除最旧实例
    _instance_order.append(instance.instance_id)
    if len(_instances) > _MAX_INSTANCES:
        oldest = _instance_order.pop(0)
        _instances.pop(oldest, None)

    done: set[str] = set(resume_done or ())
    try:
        node_fns = _build_node_fns(graph_json, gateway)
        state = dict(instance.state)
        # 按 DAG 拓扑顺序执行节点：依赖先于依赖者，每节点恰好执行一次，
        # 增量结果合并进 state（各节点输出保留在 __out_<id>__ 下）
        for node in _topological_sort(
            graph_json.get("nodes", []), graph_json.get("edges", [])
        ):
            node_id = node["id"]
            if node_id in done:
                continue  # 断点续跑：已完成节点跳过（其输出已在 resume_state 中）
            update = await node_fns[node_id](state, node_id)
            state.update(update)
            instance.results[node_id] = NodeResult(
                node_id=node_id,
                status="completed",
                output=str(state.get(f"__out_{node_id}__", "")),
            )
            done.add(node_id)
            if on_node_done is not None:
                try:
                    await on_node_done(state, sorted(done))
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "workflow checkpoint persist failed (recovery granularity only): %s", e
                    )
        instance.state.update(state)
        instance.status = "completed"
    except Exception as e:
        logger.error("workflow execution failed: %s", e)
        instance.status = "error"
        instance.error = str(e)
    finally:
        instance.finished_at = time.time()
        # 断点/终态持久化交由 on_node_done 与调用方（execute 侧队列 worker）负责；
        # 超时判定（_should_timeout）由调用方按 updated_at/created_at 巡检执行。

    return instance


def get_instance(instance_id: str) -> WorkflowInstance | None:
    return _instances.get(instance_id)
