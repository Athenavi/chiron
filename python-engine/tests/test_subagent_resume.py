"""`resume_subagent` 工具（R5(b)）—— 带着**已记录的步骤**继续 / 从某一步分叉。

与 `rerun_subagent` 的差别只有起始上下文：重跑是"按原任务再派一次"，续跑是"沿着它自己
已经产生的步骤接着做"。设计见 `docs/subagent-resume-design.md`，本节钉住它的六条验收：

1. **默认关**：未启用时任何调用都得到明确的"未启用"错误（不触 DB）；
2. **血缘可查**：派发时带上 `resumed_from` / `resume_at_step`，返回体也回显；
3. **历史真的带上**：步骤被渲染进 `resume_context`，带标记与"数据不是指令"声明；
4. **原 run 一个字不改**：本工具只 SELECT —— 全程不产生任何 UPDATE；
5. **跨租户 / 跨会话拒绝**：访问谓词与 `read_subagent_result` / `rerun_subagent` 同一份；
6. **只许终态 run**（`running` 已有主）且**写权限只能收紧**。
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.subagent import resume as resume_mod
from app.tools import subagent_resume


class _FakePool:
    def __init__(self, row=None, steps=None, error: Exception | None = None):
        self._row = row
        self._steps = steps or []
        self._error = error
        self.queries: list[tuple] = []
        self.updates: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        if self._error:
            raise self._error
        return self._row

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self._steps

    async def execute(self, sql, *args):
        # 本工具**不该**写任何东西：任何 execute 都是一次"改动原 run"的嫌疑
        self.updates.append((sql, args))
        return "UPDATE 1"


def _row(**overrides):
    row = {
        "id": "rs_old",
        "task": "把 utils.py 里的日期解析改成时区安全",
        "profile_name": "reviewer",
        "read_only": True,
        "status": "lost",
        "depth": 1,
    }
    row.update(overrides)
    return row


def _step(seq: int, content: str, role: str = "assistant", kind: str = "message"):
    return {
        "seq": seq,
        "kind": kind,
        "role": role,
        "tool_name": "",
        "content": content,
    }


@pytest.fixture(autouse=True)
def ctx(monkeypatch):
    monkeypatch.setattr(subagent_resume, "get_tenant_id", lambda: "t1")
    monkeypatch.setattr(subagent_resume, "get_user_id", lambda: "u1")
    monkeypatch.setattr(subagent_resume, "get_session_id", lambda: "s1")
    monkeypatch.setattr(settings, "subagent_resume_enabled", True)


def _patch_pool(monkeypatch, pool):
    monkeypatch.setattr("app.db.get_pool", lambda: pool)
    return pool


def _patch_delegate(monkeypatch, captured: list[dict]):
    async def _fake(**kwargs):
        captured.append(kwargs)
        return {
            "status": "async_launched",
            "isAsync": True,
            "run_id": "rs_new",
            "result_ref": "rs_new",
            "note": "detail",
        }

    monkeypatch.setattr("app.tools.subagent.subagent", _fake)


# ── ① 默认关 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_by_default_is_explicit(monkeypatch):
    """默认关 ⇒ 明确的"未启用"错误（而不是静默失败或"看起来跑了"）。"""
    monkeypatch.setattr(settings, "subagent_resume_enabled", False)
    pool = _FakePool(row=_row())
    _patch_pool(monkeypatch, pool)

    result = await subagent_resume.resume_subagent("rs_old", "接着做")

    assert "disabled" in result["error"] and "SUBAGENT_RESUME_ENABLED" in result["error"]
    assert pool.queries == [], "未启用时不该碰数据库"


# ── ② 血缘 + ③ 历史真的带上 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lineage_and_history_are_passed(monkeypatch):
    captured: list[dict] = []
    pool = _FakePool(
        row=_row(),
        steps=[_step(1, "读了 utils.py"), _step(2, "发现三处 naive 时间比较", role="tool")],
    )
    _patch_pool(monkeypatch, pool)
    _patch_delegate(monkeypatch, captured)

    result = await subagent_resume.resume_subagent("rs_old", "把剩下两处也改掉")

    assert "error" not in result, result
    assert captured, "必须真的派发一个新 run"
    kwargs = captured[0]
    assert kwargs["resumed_from"] == "rs_old"
    assert kwargs["resume_at_step"] is None, "at_step=0 ⇒ 续到底（NULL）"
    assert kwargs["task"] == "把剩下两处也改掉"
    text = kwargs["resume_context"]
    assert "读了 utils.py" in text and "发现三处 naive 时间比较" in text
    assert '<resumed-from run_id="rs_old"' in text
    assert "不是指令" in text, "带来的步骤必须带信任声明（它离系统提示词更近了）"
    # 返回体把"续跑这件事"回显给模型（否则它无法在后续对话里引用）
    assert result["resumed_from"] == "rs_old"
    assert result["resumed_steps"] == 2
    assert result["resume_at_step"] is None


@pytest.mark.asyncio
async def test_at_step_forks_and_filters_in_sql(monkeypatch):
    captured: list[dict] = []
    pool = _FakePool(row=_row(), steps=[_step(1, "第一步")])
    _patch_pool(monkeypatch, pool)
    _patch_delegate(monkeypatch, captured)

    result = await subagent_resume.resume_subagent("rs_old", "换一条路", at_step=5)

    assert captured[0]["resume_at_step"] == 5
    assert result["resume_at_step"] == 5
    steps_sql = next(sql for sql, _args in pool.queries if "subagent_run_steps" in sql)
    assert "$2::int = 0 OR seq < $2::int" in steps_sql, "分叉点必须由 SQL 过滤（不靠宿主裁）"
    assert pool.queries[1][1] == ("rs_old", 5), "at_step 要真的作为参数传下去"


# ── ④ 原 run 一个字不改 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_original_run_is_never_written(monkeypatch):
    """续跑 = 产生**新 run**；被续跑的 run 是证据，任何时候都不该被 UPDATE。"""
    captured: list[dict] = []
    pool = _FakePool(row=_row(), steps=[_step(1, "x")])
    _patch_pool(monkeypatch, pool)
    _patch_delegate(monkeypatch, captured)

    await subagent_resume.resume_subagent("rs_old", "继续")

    assert pool.updates == [], "本工具只读，不得对原 run 做任何写入"


# ── ⑤ 跨租户 / 跨会话拒绝 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_access_predicate_is_in_the_query(monkeypatch):
    """租户/会话隔离靠**查询谓词**（与另两条读路径同一份），不是靠宿主事后过滤。"""
    captured: list[dict] = []
    pool = _FakePool(row=_row(), steps=[])
    _patch_pool(monkeypatch, pool)
    _patch_delegate(monkeypatch, captured)

    await subagent_resume.resume_subagent("rs_old", "继续")

    sql, args = pool.queries[0]
    assert "tenant_id = $2" in sql
    assert "(user_id = $3 OR user_id IS NULL)" in sql
    assert "root_session_id = $4" in sql
    assert args == ("rs_old", "t1", "u1", "s1")


@pytest.mark.asyncio
async def test_run_from_another_tenant_or_session_is_not_found(monkeypatch):
    """别的租户 / 别的会话的 run ⇒ 谓词不命中 ⇒ "not found"（不说"存在但你没权限"）。"""
    pool = _FakePool(row=None)
    _patch_pool(monkeypatch, pool)

    result = await subagent_resume.resume_subagent("rs_other", "继续")

    assert "not found" in result["error"]
    assert "another tenant/session" in result["error"]


# ── ⑥ 只许终态 + 写权限只能收紧 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_running_run_cannot_be_resumed(monkeypatch):
    captured: list[dict] = []
    _patch_pool(monkeypatch, _FakePool(row=_row(status="running")))
    _patch_delegate(monkeypatch, captured)

    result = await subagent_resume.resume_subagent("rs_old", "继续")

    assert "already has an owner" in result["error"]
    assert captured == [], "running 的 run 不该被续跑（会得到两份并发作业）"


@pytest.mark.asyncio
@pytest.mark.parametrize(("read_only", "expected"), [(True, False), (False, True)])
async def test_write_permission_can_only_tighten(monkeypatch, read_only, expected):
    captured: list[dict] = []
    _patch_pool(monkeypatch, _FakePool(row=_row(read_only=read_only), steps=[_step(1, "x")]))
    _patch_delegate(monkeypatch, captured)

    await subagent_resume.resume_subagent("rs_old", "继续")

    assert captured[0]["allow_write"] is expected, "续跑不是提权通道：只读的 run 续跑后仍只读"


@pytest.mark.asyncio
async def test_instruction_is_required(monkeypatch):
    """续跑要有**新的目标** —— 否则那是重跑（`rerun_subagent`）的活。"""
    _patch_pool(monkeypatch, _FakePool(row=_row()))

    result = await subagent_resume.resume_subagent("rs_old", "   ")

    assert "instruction is required" in result["error"]
    assert "rerun_subagent" in result["error"], "错误里要把该用哪个工具说清楚"


@pytest.mark.asyncio
async def test_negative_at_step_is_refused(monkeypatch):
    _patch_pool(monkeypatch, _FakePool(row=_row()))

    result = await subagent_resume.resume_subagent("rs_old", "继续", at_step=-1)

    assert "at_step must be >= 0" in result["error"]


# ── 重建本身（纯函数）──────────────────────────────────────────────────────


def test_build_context_keeps_the_recent_steps():
    """超限时**保近处**：远处的历史对"接着做"价值更低。"""
    rows = [_step(i, f"step-{i}") for i in range(1, 11)]

    ctx = resume_mod.build_resume_context(rows, run_id="rs1", max_steps=3)

    assert ctx.used_steps == 3
    for late in ("step-8", "step-9", "step-10"):
        assert late in ctx.text
    assert "step-1 " not in ctx.text and ctx.text.count("step-1:") == 0
    assert ctx.dropped_steps == 7
    assert ctx.truncated is True
    assert "已按上限省略" in ctx.text, "截断必须**显式标注**"


def test_build_context_redacts_and_truncates_a_single_step():
    rows = [
        _step(1, "key=sk-abcdefghijklmnopqrstuvwx"),
        _step(2, "x" * (resume_mod.STEP_MAX_CHARS + 50)),
    ]

    ctx = resume_mod.build_resume_context(rows, run_id="rs1")

    assert "sk-abcdefghijklmnopqrstuvwx" not in ctx.text
    assert "...(truncated)" in ctx.text
    assert ctx.redacted_hits >= 1
    assert ctx.truncated_steps == 1


def test_build_context_without_steps_is_empty():
    ctx = resume_mod.build_resume_context([], run_id="rs1")

    assert not ctx
    assert ctx.text == ""
