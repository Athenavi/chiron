"""R1（`vendor/规划.md` §3.5）：宿主证据（host receipts）。

对齐参照是 Reasonix 的 `subagent_report.go`。三条语义是它的**测试口径**，也是本文件的重点：

1. **只读子 Agent 保持沉默** —— 无可报告事实时不产收据（沉默是正确，不是漏报）；
2. **失败的命令也记** —— 不掩盖；
3. **只读承诺被打破 ⇒ 违规** —— 这是**唯一**能证明"宿主核验"真的有效的判定。

另有一条同等重要的诚实性要求：**快照没看全时必须说**，否则"什么都没变"会被误读为已确认。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.subagent.receipts import (
    RECEIPTS_MARKER,
    ExecObservation,
    HostReceipts,
    append_host_receipts,
    build_receipts,
    capture_workspace,
    diff_workspace,
    format_host_receipts,
    split_host_receipts,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── 1. 快照与差分（纯逻辑）────────────────────────────────────────────


def test_capture_and_diff_detect_created_modified_removed(tmp_path):
    _write(tmp_path / "keep.txt", "same")
    _write(tmp_path / "change.txt", "before")
    _write(tmp_path / "gone.txt", "bye")
    before = capture_workspace(tmp_path)

    _write(tmp_path / "change.txt", "after")  # modified
    os.remove(tmp_path / "gone.txt")  # removed
    _write(tmp_path / "new.txt", "hi")  # created
    after = capture_workspace(tmp_path)

    diff = diff_workspace(before, after)

    assert diff.created == ("new.txt",)
    assert diff.modified == ("change.txt",)
    assert diff.removed == ("gone.txt",)
    assert diff.total == 3


def test_untouched_workspace_diffs_empty(tmp_path):
    _write(tmp_path / "a.txt", "x")
    before = capture_workspace(tmp_path)

    diff = diff_workspace(before, capture_workspace(tmp_path))

    assert diff.is_empty


def test_same_size_but_new_mtime_counts_as_modified(tmp_path):
    """只比大小会漏掉"内容变了但长度一样" —— 必须看 mtime。"""
    _write(tmp_path / "f.txt", "aaaa")
    before = capture_workspace(tmp_path)
    target = tmp_path / "f.txt"
    os.utime(target, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 1_000_000))

    assert diff_workspace(before, capture_workspace(tmp_path)).modified == ("f.txt",)


def test_noise_dirs_are_skipped(tmp_path):
    _write(tmp_path / "__pycache__/junk.pyc", "x")
    _write(tmp_path / ".git/HEAD", "ref")

    snap = capture_workspace(tmp_path)

    assert snap.entries == {}


def test_snapshot_is_bounded_and_says_so(tmp_path):
    for i in range(6):
        _write(tmp_path / f"f{i}.txt", "x")

    snap = capture_workspace(tmp_path, max_entries=3)

    assert snap.truncated is True
    assert len(snap.entries) == 3


def test_missing_root_is_not_an_error(tmp_path):
    snap = capture_workspace(tmp_path / "nope")

    assert not snap.entries and snap.truncated is False


# ── 2. 沉默规则 ────────────────────────────────────────────────────────


def test_read_only_child_with_no_changes_stays_silent():
    """对齐 Reasonix 的 `TestHostReceiptsStaySilentForReadOnlyChildren`。"""
    receipts = build_receipts(
        before=capture_workspace(Path(".")),
        after=capture_workspace(Path(".")),
        execs=ExecObservation(),
        read_only=True,
    )

    assert receipts.should_report is False
    assert format_host_receipts(receipts) == ""
    assert append_host_receipts("干完了", receipts) == "干完了"


def test_read_only_child_that_actually_read_files_is_silent(tmp_path):
    """读文件不算"执行" —— 只读子 Agent 读了很多文件也不该产收据。"""
    receipts = HostReceipts(
        diff=diff_workspace(capture_workspace(tmp_path), capture_workspace(tmp_path)),
        execs=ExecObservation(),  # 读操作不进这个计数
        read_only=True,
    )

    assert receipts.should_report is False


# ── 3. 失败也记 ────────────────────────────────────────────────────────


def test_failed_commands_are_reported(tmp_path):
    """对齐 `TestHostReceiptsRecordFailedCommands`：失败必须出现，不能只报成功。"""
    snap = capture_workspace(tmp_path)
    receipts = build_receipts(
        before=snap, after=snap, execs=ExecObservation(executions=3, failures=2), read_only=False
    )

    text = format_host_receipts(receipts)

    assert "执行 3 次" in text
    assert "失败 2 次" in text


# ── 4. 违规：只读却改了东西（核心）────────────────────────────────────


def test_read_only_child_that_wrote_files_is_a_violation(tmp_path):
    """**本模块最重要的断言**：只读承诺被打破必须被宿主发现。

    这就是"宿主核验"相对"只信自述"的全部价值 —— 若这条测不出东西，收据就只是装饰。
    """
    before = capture_workspace(tmp_path)
    _write(tmp_path / "sneaky.txt", "我偷偷写了")
    after = capture_workspace(tmp_path)

    receipts = build_receipts(before=before, after=after, execs=ExecObservation(), read_only=True)

    assert receipts.violations == ("created: sneaky.txt",)
    text = format_host_receipts(receipts)
    assert "违规" in text and "只读" in text
    assert "sneaky.txt" in text


def test_writable_child_writes_are_not_violations(tmp_path):
    """有写权限时改动是**预期**的，只陈述、不判违规。"""
    before = capture_workspace(tmp_path)
    _write(tmp_path / "ok.txt", "x")

    receipts = build_receipts(
        before=before, after=capture_workspace(tmp_path), execs=ExecObservation(), read_only=False
    )

    assert receipts.violations == ()
    assert "新建（1）" in format_host_receipts(receipts)


# ── 5. 诚实性：没看全必须说 ───────────────────────────────────────────


def test_incomplete_snapshot_is_always_reported(tmp_path):
    """快照没看全 ⇒ 即便观测到"无变化"也必须报，否则会被读成"已确认没变"。"""
    _write(tmp_path / "f.txt", "x")  # 有文件才会走到上限判断
    truncated = capture_workspace(tmp_path, max_entries=1)

    receipts = build_receipts(
        before=truncated, after=truncated, execs=ExecObservation(), read_only=True
    )

    assert receipts.incomplete is True
    assert receipts.should_report is True
    assert "未看全" in format_host_receipts(receipts)


def test_missing_snapshot_marks_incomplete_not_clean():
    receipts = build_receipts(before=None, after=None, execs=ExecObservation(), read_only=True)

    assert receipts.incomplete is True
    assert receipts.should_report is True


# ── 6. 有界与拆分 ─────────────────────────────────────────────────────


def test_long_lists_are_folded(tmp_path):
    before = capture_workspace(tmp_path)
    for i in range(20):
        _write(tmp_path / f"f{i:02d}.txt", "x")
    receipts = build_receipts(
        before=before, after=capture_workspace(tmp_path), execs=ExecObservation(), read_only=False
    )

    text = format_host_receipts(receipts, max_listed=5)

    assert "另有 15 个" in text


def test_split_separates_prose_from_attestation(tmp_path):
    before = capture_workspace(tmp_path)
    _write(tmp_path / "a.txt", "x")
    receipts = build_receipts(
        before=before, after=capture_workspace(tmp_path), execs=ExecObservation(), read_only=False
    )

    combined = append_host_receipts("我的结论是……", receipts)
    prose, attestation = split_host_receipts(combined)

    assert prose == "我的结论是……"
    assert attestation.startswith(RECEIPTS_MARKER)


def test_split_without_receipts_returns_answer_unchanged():
    assert split_host_receipts("只有散文") == ("只有散文", "")


def test_receipts_state_the_limitation(tmp_path):
    """必须写明"工作区为进程级共享、并发改动无法归因" —— 把看不清说成看清比不给收据更糟。"""
    before = capture_workspace(tmp_path)
    _write(tmp_path / "a.txt", "x")
    receipts = build_receipts(
        before=before, after=capture_workspace(tmp_path), execs=ExecObservation(), read_only=False
    )

    text = format_host_receipts(receipts)

    assert "宿主观测" in text
    assert "并发" in text


# ── 7. 端到端：runner 真的把收据附进摘要 ──────────────────────────────


@pytest.mark.asyncio
async def test_runner_appends_receipts_to_summary(monkeypatch, tmp_path):
    """接线验证：摘要里出现收据，且收据反映**工作区**的真实变化（不是子 Agent 说了什么）。"""
    from app.agent import runtime as runtime_mod
    from app.agent.subagent_runner import SubAgentRunner
    from app.tools import sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "workspace_dir", lambda: tmp_path)

    async def fake_run(self, task):  # noqa: ANN001
        # 子 Agent "跑"了两个执行类工具，其中一个失败；并写了一个文件
        (tmp_path / "made.txt").write_text("x", encoding="utf-8")
        yield runtime_mod.AgentEvent(type="tool_call", tool_name="shell_exec")
        yield runtime_mod.AgentEvent(type="tool_result", tool_name="shell_exec", content="ok")
        yield runtime_mod.AgentEvent(type="tool_call", tool_name="run_code")
        yield runtime_mod.AgentEvent(type="tool_result", error="boom")
        yield runtime_mod.AgentEvent(type="text", content="我做完了")

    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", fake_run)

    class _Store:
        async def start_run(self, *a, **k):  # noqa: ANN002, ANN003
            return None

        async def add_step(self, *a, **k):  # noqa: ANN002, ANN003
            return None

        async def flush_steps(self, *a, **k):  # noqa: ANN002, ANN003
            return None

        async def finish_run(self, *a, **k):  # noqa: ANN002, ANN003
            return None

        async def mark_lifecycle(self, *a, **k):  # noqa: ANN002, ANN003
            return None

    runner = SubAgentRunner(store=_Store(), gateway=object(), allow_write=True)
    result = await runner.run("干点活", mode="normal", max_turns=1)

    assert RECEIPTS_MARKER in result.summary
    assert "made.txt" in result.summary  # 宿主自己看到的变化
    assert "失败 1 次" in result.summary  # 失败也记
