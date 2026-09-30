"""E3：把同一批固件跑在 `vendor/deepagents` 上，产出**同格式**报告。

对标的前提是**同一把尺子**，所以这里不重写执行器 —— 只实现一个 `SubmitFn`
（`evals.runner` 的注入点），其余（落固件 → 读产物 → 判定 → 聚合）全部复用 Chiron 侧那套。
换掉其中任何一环，`compare` 出来的差异就分不清是"能力差"还是"评分口径差"。

三条边界：

1. **延迟 import**：`langchain` / `langgraph` / `deepagents` 都**不在**本仓库的依赖清单里
   （E3 的前置：独立可选环境、不阻塞主 CI）。顶层 import 会让所有人为对标付安装成本，也会让
   主测试套件在没装它们的机器上直接炸。
2. **产物必须落成真实文件**：用 `FilesystemBackend(root_dir=工作区, virtual_mode=True)`，而不是
   默认的 `StateBackend`（后者把文件存在 LangGraph state 里）—— 否则 `file_contains` /
   `file_equals` 这类断言在两侧根本不可比。
3. **步数口径对齐**：Chiron 侧的 `steps` = `llm_call` span 数（模型调用回合数），这里取
   **AIMessage 的数量** —— 两者都是"模型往返轮数"。若改用工具调用数，两边就不可比了。

用法（需先装依赖并配模型密钥；见 README 的 E3 说明）：

```bash
python -m evals.adapters.deepagents_agent --suite full --tier full \\
    --model gpt-4o-mini --out out/deepagents-full.json
python -m evals.cli compare out/chiron-full.json out/deepagents-full.json
```
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from evals.assertions import Observation
from evals.firmware import Task, load_suite, select_tasks
from evals.observe import _strip_thinking
from evals.report import build_report
from evals.runner import EvalRunner

#: 与 Chiron 侧尽量对齐的 system 提示词。
#:
#: **已知的不对等**：Chiron 经网关还会带上系统记忆、技能目录、护栏等系统段，deepagents 侧只有一个
#: 静态提示词。这是对标固有的不对等项，不藏起来 —— 结论里要把它写成"Chiron 的额外能力"，
#: 而不是"deepagents 太弱"。
SYSTEM_PROMPT = (
    "You are a capable coding agent working in a sandboxed workspace. "
    "Use the available tools to read and modify files. Be concise in your final answer."
)


def _import_deepagents() -> tuple[Any, Any, Any]:
    """延迟导入对标所需的依赖；缺失时给出**可执行的**安装提示。"""
    try:
        from deepagents import create_deep_agent  # type: ignore[import-not-found]
        from deepagents.backends import FilesystemBackend  # type: ignore[import-not-found]
        from langchain_core.messages import HumanMessage  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — 取决于可选环境
        raise SystemExit(
            "对比评测需要 deepagents 的依赖（langchain / langgraph / deepagents）。\n"
            "它们**不在**主依赖清单里（E3 的前置：独立可选环境）。参考安装：\n"
            "  pip install -e vendor/deepagents/libs/deepagents langchain-openai\n"
            f"原始错误：{exc}"
        ) from exc
    return create_deep_agent, FilesystemBackend, HumanMessage


def _text_of(content: Any) -> str:
    """把消息内容取成纯文本（LangChain 的 content 可能是 `str` 或多模态块列表）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content or "")


def observation_from_messages(messages: list[Any]) -> Observation:
    """把 LangGraph 的消息列表归约为 `Observation`（**纯函数**，因此可单测）。

    只看两类消息：
    * `AIMessage` —— 累一步、取正文与工具请求、累加 usage；
    * `ToolMessage(status="error")` —— 记为**工具被拒/出错**（护栏证据就靠它）。
    """
    obs = Observation()
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    steps = 0
    tokens = 0

    for message in messages or []:
        name = type(message).__name__
        if name in ("AIMessage", "AIMessageChunk"):
            steps += 1
            texts.append(_strip_thinking(_text_of(getattr(message, "content", ""))))
            for call in getattr(message, "tool_calls", None) or []:
                calls.append(
                    {
                        "name": str(call.get("name", "")),
                        "args": json.dumps(call.get("args") or {}, ensure_ascii=False),
                        "error": None,
                    }
                )
            usage = getattr(message, "usage_metadata", None) or {}
            tokens += int(usage.get("input_tokens") or 0) + int(
                usage.get("output_tokens") or 0
            )
        elif name == "ToolMessage":
            status = str(getattr(message, "status", "") or "")
            if status == "error" and calls:
                calls[-1]["error"] = _text_of(getattr(message, "content", ""))[:200] or "tool error"

    obs.final_text = "".join(texts)
    obs.tool_calls = calls
    obs.steps = steps
    obs.tokens = tokens
    return obs


class DeepAgentsSubmit:
    """`SubmitFn` 实现：一条固件 = 在 deepagents 上跑一轮。"""

    def __init__(self, model: str, *, timeout: float = 600.0) -> None:
        self._model = model
        self._timeout = timeout
        create, backend_cls, human = _import_deepagents()
        self._create_deep_agent = create
        self._FilesystemBackend = backend_cls
        self._HumanMessage = human

    async def __call__(self, task: Task, workspace: Path) -> Observation:
        # `virtual_mode=True` 把工作区当作虚拟根：相对路径锚定在它下面，且拒绝 `..` 越界 ——
        # 与 Chiron 侧的沙箱语义对齐，断言才可比。
        backend = self._FilesystemBackend(root_dir=str(workspace), virtual_mode=True)
        agent = self._create_deep_agent(
            model=self._model, backend=backend, system_prompt=SYSTEM_PROMPT
        )
        started = time.monotonic()
        result = await asyncio.wait_for(
            agent.ainvoke({"messages": [self._HumanMessage(content=task.prompt)]}),
            timeout=self._timeout,
        )
        observation = observation_from_messages(
            list((result or {}).get("messages") or [])
        )
        observation.wall_ms = int((time.monotonic() - started) * 1000)
        return observation


async def run_suite(
    *, suite_path: Path, tier: str, model: str, out: Path, repeat: int = 1
) -> int:
    tasks = load_suite(suite_path)
    tasks = select_tasks(tasks, tier) if tier else tasks
    if not tasks:
        print(f"no tasks selected (suite={suite_path.name} tier={tier})", file=sys.stderr)
        return 1

    submit = DeepAgentsSubmit(model)
    root = out.parent / "deepagents-workspaces"
    root.mkdir(parents=True, exist_ok=True)
    runner = EvalRunner(submit, root)
    results = await runner.run_suite(tasks, repeat=repeat)

    report = build_report(
        suite=suite_path.stem, agent="deepagents", results=results, repeat=repeat
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"deepagents report written: {out}")
    print(f"  pass@k = {report.pass_at_k:.2%}  avg@k = {report.avg_at_k:.2%}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals-deepagents",
        description="E3：用同一批固件跑 vendor/deepagents，产出可与 Chiron 对照的报告",
    )
    parser.add_argument("--suite", default="full", help="固件名（不含 .json）")
    parser.add_argument("--tier", default="full", choices=["smoke", "full", ""])
    parser.add_argument("--model", required=True, help="传给 create_deep_agent 的模型名")
    parser.add_argument("--out", required=True, help="报告输出路径")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args(argv)

    suites_dir = Path(__file__).resolve().parent.parent / "suites"
    suite_path = suites_dir / f"{args.suite}.json"
    if not suite_path.is_file():
        raise SystemExit(f"suite not found: {suite_path}")

    return asyncio.run(
        run_suite(
            suite_path=suite_path,
            tier=args.tier,
            model=args.model,
            out=Path(args.out),
            repeat=args.repeat,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
