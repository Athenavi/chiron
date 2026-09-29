"""评测骨架（E1/E2）的回归测试。

评测套件自己也需要回归网 —— 否则"断言引擎坏了"会表现成"agent 全挂了"，而两者必须能区分。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.assertions import Observation, evaluate
from evals.firmware import Assertion, Efficiency, load_suite, parse_task, select_tasks
from evals.observe import observation_from_events
from evals.report import build_report
from evals.runner import EvalRunner
from evals.submit import ScriptedSubmit

SUITES = Path(__file__).resolve().parents[1] / "evals" / "suites"


# ── firmware ──────────────────────────────────────────────────────────────


def test_parse_task_rejects_unknown_assertion_kind():
    with pytest.raises(ValueError, match="unknown assertion kind"):
        parse_task(
            {
                "id": "t1",
                "category": "file_edit",
                "prompt": "x",
                "assertions": [{"kind": "made_up_kind"}],
            }
        )


def test_parse_task_rejects_unknown_category_and_requires_assertions():
    with pytest.raises(ValueError, match="unknown category"):
        parse_task({"id": "t1", "category": "nope", "prompt": "x", "assertions": [{"kind": "tool_called", "tool": "read_file"}]})

    with pytest.raises(ValueError, match="at least one assertion"):
        parse_task({"id": "t1", "category": "file_edit", "prompt": "x", "assertions": []})


def test_select_tasks_filters_by_tier():
    tasks = load_suite(SUITES / "smoke.json")
    assert tasks, "smoke 固件不应为空"
    smoke = select_tasks(tasks, "smoke")
    full = select_tasks(tasks, "full")
    assert len(smoke) == len(tasks)  # smoke 集内全部任务都标了 smoke
    assert len(full) < len(smoke)  # full 只含同时标了 full 的那部分


# ── observe：事件流归约 ────────────────────────────────────────────────────


def test_thinking_is_stripped_from_final_text():
    obs = observation_from_events(
        [
            {"type": "text", "content": "[thinking]内部推理[/thinking]最终回答"},
            {"type": "done", "input_tokens": 1, "output_tokens": 2},
        ]
    )
    assert obs.final_text == "最终回答"
    assert "内部推理" not in obs.final_text


def test_steps_count_llm_calls_not_tool_calls():
    """步数口径：一轮里可以有多个工具调用，但它们只算一次模型调用。"""
    obs = observation_from_events(
        [
            {"type": "tool_call", "tool_call_id": "c1", "tool_name": "read_file"},
            {"type": "tool_call", "tool_call_id": "c2", "tool_name": "read_file"},
            {"type": "tool_call", "tool_call_id": "c3", "tool_name": "read_file"},
            {"type": "trace_span", "span_name": "llm_call"},
            {"type": "done", "input_tokens": 5, "output_tokens": 5},
        ]
    )
    assert obs.steps == 1
    assert len(obs.tool_calls) == 3
    assert obs.tokens == 10


def test_tool_denied_is_distinguished_from_not_called():
    """工具"被拒"必须与"没调用"区分开 —— 前者才是护栏证据。"""
    denied = observation_from_events(
        [
            {"type": "tool_call", "tool_call_id": "c1", "tool_name": "shell_exec"},
            {
                "type": "tool_result",
                "tool_call_id": "c1",
                "tool_name": "shell_exec",
                "content": json.dumps({"error": "command blocked"}),
            },
        ]
    )
    ok = observation_from_events(
        [
            {"type": "tool_call", "tool_call_id": "c1", "tool_name": "shell_exec"},
            {"type": "tool_result", "tool_call_id": "c1", "tool_name": "shell_exec", "content": "{}"},
        ]
    )
    obs_denied = evaluate((Assertion("tool_denied", tool="shell_exec"),), Efficiency(), denied)
    obs_ok = evaluate((Assertion("tool_denied", tool="shell_exec"),), Efficiency(), ok)
    assert obs_denied.passed is True
    assert obs_ok.passed is False


# ── assertions ────────────────────────────────────────────────────────────


def test_efficiency_never_fails_the_task():
    """efficiency 只记录：超出期望不能把有效解判成失败。"""
    obs = Observation(steps=99, tool_calls=[{"name": "x"}], tokens=9999, wall_ms=99999)
    result = evaluate(
        (Assertion("event_emitted", value="done"),),
        Efficiency(max_steps=1, max_tool_calls=1, max_tokens=1, max_wall_ms=1),
        obs,
    )
    # event_emitted 不满足 → 失败（这是 success 断言）
    assert result.passed is False

    obs2 = Observation(events=[{"type": "done"}], steps=99, tokens=9999)
    result2 = evaluate(
        (Assertion("event_emitted", value="done"),),
        Efficiency(max_steps=1, max_tokens=1),
        obs2,
    )
    assert result2.passed is True
    assert result2.efficiency_notes, "超出期望应留下 note"
    assert any("steps" in n for n in result2.efficiency_notes)


def test_file_assertions_read_artifacts():
    obs = Observation(files={"a.txt": "hello world"})
    assert evaluate((Assertion("file_contains", path="a.txt", value="world"),), Efficiency(), obs).passed
    assert not evaluate((Assertion("file_contains", path="a.txt", value="nope"),), Efficiency(), obs).passed
    assert evaluate((Assertion("file_equals", path="a.txt", value="hello world"),), Efficiency(), obs).passed
    # 产物缺失 = 不通过（而不是"跳过"）
    assert not evaluate((Assertion("file_contains", path="missing.txt", value="x"),), Efficiency(), obs).passed


# ── runner ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_materializes_fixtures_and_reads_artifacts(tmp_path: Path):
    task = parse_task(
        {
            "id": "edit-config",
            "category": "file_edit",
            "tiers": ["smoke"],
            "prompt": "改成 true",
            "fixture_files": {"config.ini": "debug=false\n"},
            "assertions": [{"kind": "file_contains", "path": "config.ini", "value": "debug=true"}],
        }
    )
    seen: dict[str, str] = {}

    async def submit(t, workspace: Path) -> Observation:
        # 固件必须已被落盘（runner 的职责）
        seen["fixture"] = (workspace / "config.ini").read_text(encoding="utf-8")
        (workspace / "config.ini").write_text("debug=true\n", encoding="utf-8")
        return Observation(events=[{"type": "done"}])

    result = await EvalRunner(submit, tmp_path).run_task(task)
    assert seen["fixture"] == "debug=false\n", "固件未落盘"
    assert result.passed is True


@pytest.mark.asyncio
async def test_runner_isolates_task_failure(tmp_path: Path):
    """单条任务抛错不该中断整个套件（也不该被当成通过）。"""
    task = parse_task(
        {
            "id": "boom",
            "category": "guardrail",
            "tiers": ["smoke"],
            "prompt": "x",
            "assertions": [{"kind": "guardrail_blocked"}],
        }
    )

    async def submit(t, workspace: Path) -> Observation:
        raise RuntimeError("gateway down")

    result = await EvalRunner(submit, tmp_path).run_task(task)
    assert result.passed is False
    assert "gateway down" in result.error


# ── 端到端：smoke 集在替身 provider 下必须全过 ─────────────────────────────


@pytest.mark.asyncio
async def test_smoke_suite_passes_with_scripted_submit(tmp_path: Path):
    """这是 PR 门禁要跑的形态：零密钥、零费用、可判定。"""
    tasks = select_tasks(load_suite(SUITES / "smoke.json"), "smoke")
    scripts = json.loads((SUITES / "smoke.scripted.json").read_text(encoding="utf-8"))
    scripts = {k: v for k, v in scripts.items() if isinstance(v, dict)}

    runner = EvalRunner(ScriptedSubmit(scripts), tmp_path)
    results = await runner.run_suite(tasks)
    report = build_report(suite="smoke", agent="scripted", results=results)

    failures = [t for t in report.tasks if not t.ever_passed]
    assert not failures, f"替身脚本未覆盖这些任务：{[t.task_id for t in failures]}"
    assert report.pass_at_k == 1.0
    assert report.avg_at_k == 1.0
    # 每条任务都必须有对应脚本（否则断言"通过"是假的）
    assert {t.task_id for t in report.tasks} == set(scripts)
