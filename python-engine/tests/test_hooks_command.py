"""批 G+ 批次 2b：`command` 形态（**运维声明的本地命令** hook）回归。

对应 `docs/hook-protocol-design.md` §7 的验收项③（只能收紧）/④（退出码约定）/⑤（fail-open + 留痕）
/⑦（执行入口清单），外加 §4.3 选项 **(b)** 独有的三条：

1. **默认关**：`hooks_allow_commands=false` 时条目**被拒**（不是"配了但不生效"）；
2. **独立 allowlist**：可执行文件必须落在 `hooks_command_allowlist` 内，且这张表**不是**
   面向模型的那张（`git` 在沙箱白名单里、却不在 hook allowlist 里 ⇒ 拒绝）；
3. **允许绝对路径**：这是"让既有的 Claude Code / Codex 脚本能直接用"的前提 ——
   也是 `run_in_sandbox` 直接复用不成立的那一条（§4.3 实测表）。

用例真的**起子进程**（用 `sys.executable` 跑临时脚本），因为"能不能跑、退出码怎么解释"
是这批的对外行为本身，不能靠桩函数证明。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.hooks import (
    HANDLER_COMMAND,
    OWNER_USER,
    command_policy_error,
    events,
    hooks,
)
from app.hooks import config as hooks_config
from app.hooks.runner import split_command

#: 子进程脚本写出的文件由用例断言（证明 stdin 真的把事件上下文送进去了）。
STDIN_SCRIPT = """
import json, sys
payload = json.load(sys.stdin)
with open({target}, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False)
"""


@pytest.fixture(autouse=True)
def _hooks_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """审计落盘指向 tmp（hooks 与 exec 两条流水都要）、开关复位为默认关、清空注册表。"""
    from app.hooks import audit
    from app.tools import exec_audit

    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(exec_audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(settings, "hooks_enabled", False)
    monkeypatch.setattr(settings, "hooks_allow_user_defined", False)
    monkeypatch.setattr(settings, "hooks_timeout_seconds", 5)
    monkeypatch.setattr(settings, "hooks_event_budget_seconds", 10)
    monkeypatch.setattr(settings, "hooks_config_path", "")
    monkeypatch.setattr(settings, "hooks_allow_commands", False)
    monkeypatch.setattr(settings, "hooks_command_allowlist", "")
    hooks.clear()
    yield
    hooks.clear()


def _task(**overrides: Any) -> SimpleNamespace:
    base = {"tenant_id": "t_hookcmd", "user_id": "u_hookcmd", "session_id": "s_hookcmd"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _call(name: str = "shell_exec", arguments: str = '{"cmd": "ls"}') -> dict[str, Any]:
    return {"id": "call_1", "name": name, "arguments": arguments}


def _entries(root: Path, filename: str) -> list[dict[str, Any]]:
    path = root / filename
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _hook_entries(root: Path) -> list[dict[str, Any]]:
    return _entries(root, "hooks_audit.jsonl")


def _exec_entries(root: Path) -> list[dict[str, Any]]:
    return _entries(root, "exec_audit.jsonl")


def _script(tmp_path: Path, body: str, name: str = "hook.py") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _command(script: Path) -> str:
    """两条 token 都加引号：路径可能含空格，且这也是声明文件里的自然写法。"""
    return f'"{sys.executable}" "{script}"'


def _enable(monkeypatch: pytest.MonkeyPatch, *, allowlist: str = "", config_path: str = "") -> None:
    monkeypatch.setattr(settings, "hooks_enabled", True)
    monkeypatch.setattr(settings, "hooks_allow_commands", True)
    monkeypatch.setattr(settings, "hooks_command_allowlist", allowlist or str(sys.executable))
    monkeypatch.setattr(settings, "hooks_config_path", config_path)


# ── ① 闸口：开关 / allowlist / 绝对路径 ──────────────────────────────────────


def test_switch_off_denies_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关：`command` 形态条目**被拒**并留审计 —— 不是"配了但不生效"。"""
    monkeypatch.setattr(settings, "hooks_command_allowlist", str(sys.executable))

    assert hooks.register(events.PRE_TOOL_USE, "c", handler=HANDLER_COMMAND, command="echo hi") is False
    assert hooks.registry.all() == []
    last = _hook_entries(tmp_path)[-1]
    assert last["outcome"] == "register_denied"
    assert "hooks_allow_commands" in last["reason"]


def test_empty_allowlist_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """allowlist 为空 ⇒ **一条也不批准**（与插件命令白名单"未配置即全部拒绝"同款默认）。"""
    monkeypatch.setattr(settings, "hooks_allow_commands", True)
    monkeypatch.setattr(settings, "hooks_command_allowlist", "")

    assert hooks.register(
        events.PRE_TOOL_USE, "c", handler=HANDLER_COMMAND, command=f'"{sys.executable}" -c pass'
    ) is False
    assert hooks.registry.all() == []
    assert "allowlist" in (command_policy_error(f'"{sys.executable}" -c pass') or "")


def test_allowlist_is_not_the_model_facing_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """两张白名单**信任级不同** ⇒ 不能复制：`git` 在沙箱白名单里，但不在 hook allowlist 里就拒绝。"""
    from app.tools.sandbox import _ALLOWED_EXECUTABLES

    assert "git" in _ALLOWED_EXECUTABLES, "前提：它在**面向模型**的白名单里"
    monkeypatch.setattr(settings, "hooks_allow_commands", True)
    monkeypatch.setattr(settings, "hooks_command_allowlist", str(sys.executable))

    assert command_policy_error("git status") is not None


def test_absolute_path_entries_are_matched_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """绝对路径条目按**解析后的绝对路径**比对：裸名不等于绝对路径条目（反之亦然）。"""
    assert os.path.isabs(sys.executable), "前提：解释器路径是绝对的（本形态要支持的形态）"
    monkeypatch.setattr(settings, "hooks_allow_commands", True)
    monkeypatch.setattr(settings, "hooks_command_allowlist", str(sys.executable))

    assert command_policy_error(f'"{sys.executable}" -c pass') is None
    assert command_policy_error(f"{Path(sys.executable).name} -c pass") is not None


def test_bare_name_entries_match_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """对照：allowlist 里写裸名时，裸名命令放行（部署可以选择"任何同名可执行文件"）。"""
    monkeypatch.setattr(settings, "hooks_allow_commands", True)
    monkeypatch.setattr(settings, "hooks_command_allowlist", "  mytool , other ")

    assert command_policy_error("mytool --check") is None
    assert command_policy_error("other") is None
    assert command_policy_error("notlisted") is not None


def test_split_command_does_not_invoke_a_shell() -> None:
    """命令**永不经过 shell**：`$VAR` 不展开、`;` 不是分隔符（要 shell 特性请显式 `bash -c`）。"""
    argv, reason = split_command('sh -c "echo $HOME; rm -rf /"')

    assert reason is None
    assert argv == ["sh", "-c", "echo $HOME; rm -rf /"]
    assert split_command("")[0] == []
    assert split_command("")[1] == "empty command"


def test_tenant_cannot_register_a_command_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """选项 (b) 的前提是"**租户不可达**"：开关全开时，租户来源仍被拒。"""
    _enable(monkeypatch)

    assert hooks.register(
        events.PRE_TOOL_USE,
        "u_cmd",
        owner=OWNER_USER,
        handler=HANDLER_COMMAND,
        command=f'"{sys.executable}" -c pass',
    ) is False
    assert hooks.registry.all() == []
    assert _hook_entries(tmp_path)[-1]["outcome"] == "register_denied"


async def test_runner_rechecks_policy_before_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    """准入在**起进程之前**再判一次 —— "由 X 负责拦截"的注释要有断言打在 X 上。"""
    from app.hooks.runner import run_command_hook

    monkeypatch.setattr(settings, "hooks_allow_commands", True)
    monkeypatch.setattr(settings, "hooks_command_allowlist", str(sys.executable))

    outcome = await run_command_hook(name="ops", command="notlisted --boom", context={}, timeout=5)

    assert outcome.success is False
    assert outcome.exit_code is None, "被拦下的命令不该起进程（也就没有退出码）"
    assert (outcome.error or "").startswith("command blocked")


def test_blocked_command_is_classified_as_blocked_in_exec_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """被准入拦下的 command hook 在 exec 审计里要记 `blocked`（而不是 `error`）—— 两者处置不同。"""
    from app.hooks.manager import Hook, _record_command_exec_audit
    from app.hooks.runner import HookOutcome

    _record_command_exec_audit(
        {"tenant_id": "t1", "user_id": "u1", "session_id": "s1"},
        Hook(event=events.PRE_TOOL_USE, name="ops", handler=HANDLER_COMMAND, command="notlisted"),
        HookOutcome(
            success=False,
            output=None,
            error="command blocked: executable not in hooks_command_allowlist: notlisted",
            exit_code=None,
            duration_ms=1,
            timed_out=False,
        ),
    )

    last = _exec_entries(tmp_path)[-1]
    assert last["tool"] == "hook_command"
    assert last["outcome"] == "blocked"


# ── ② 判定语义：退出码 2 阻断 / 其余非 0 非阻断 ──────────────────────────────


async def test_exit_code_two_blocks_and_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(tmp_path, "import sys\nsys.stderr.write('blocked by ops script\\n')\nsys.exit(2)\n")
    _enable(monkeypatch)
    assert hooks.register(
        events.PRE_TOOL_USE, "ops_deny", handler=HANDLER_COMMAND, command=_command(script)
    ) is True

    blocked = await hooks.before_tool_use(task=_task(), tool_call=_call())

    assert blocked is not None, "退出码 2 必须阻断"
    assert "blocked by ops script" in blocked, "stderr 尾部要作为原因回灌"

    hook_last = _hook_entries(tmp_path)[-1]
    assert hook_last["handler"] == "command"
    assert hook_last["command"] == _command(script), "命令摘要必须落审计"
    assert hook_last["blocked"] is True
    assert hook_last["exit_code"] == 2

    exec_last = _exec_entries(tmp_path)[-1]
    assert exec_last["tool"] == "hook_command", "command 形态要进 exec 审计（多副本集中摄取只认它）"
    assert exec_last["exit_code"] == 2
    assert exec_last["outcome"] == "ok", "命令本身跑成功了（阻断是它的**判定**）"
    # 身份显式传入：这两个事件的时点可能早于 `set_tool_context`，contextvars 里是空的
    assert (exec_last["tenant"], exec_last["user"], exec_last["session"]) == (
        "t_hookcmd",
        "u_hookcmd",
        "s_hookcmd",
    )


async def test_exit_code_one_is_a_non_blocking_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """其余非 0 退出码 = **非阻断失败**：动作照走，但必须留痕。"""
    script = _script(tmp_path, "import sys\nsys.stderr.write('ops script exploded\\n')\nsys.exit(1)\n")
    _enable(monkeypatch)
    hooks.register(events.PRE_TOOL_USE, "ops_broken", handler=HANDLER_COMMAND, command=_command(script))

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None

    assert _hook_entries(tmp_path)[-1]["outcome"] == "error"
    exec_last = _exec_entries(tmp_path)[-1]
    assert exec_last["outcome"] == "error"
    assert exec_last["exit_code"] == 1
    assert "ops script exploded" in (exec_last.get("reason") or "")


async def test_exit_zero_allows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(tmp_path, "import sys\nsys.exit(0)\n")
    _enable(monkeypatch)
    hooks.register(events.PRE_TOOL_USE, "ops_ok", handler=HANDLER_COMMAND, command=_command(script))

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert _exec_entries(tmp_path)[-1]["outcome"] == "ok"


async def test_stdout_json_decision_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两种输入都接受：结构化 JSON 与退出码 2（§4.4）。"""
    script = _script(
        tmp_path,
        "import json\nprint(json.dumps({'decision': 'deny', 'reason': 'nope from stdout'}))\n",
    )
    _enable(monkeypatch)
    hooks.register(events.PRE_TOOL_USE, "ops_json", handler=HANDLER_COMMAND, command=_command(script))

    blocked = await hooks.before_tool_use(task=_task(), tool_call=_call())

    assert blocked is not None and "nope from stdout" in blocked


# ── ③ stdin 载荷（DSH / Claude Code 约定） ──────────────────────────────────


async def test_stdin_carries_the_event_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = tmp_path / "seen.json"
    script = _script(tmp_path, STDIN_SCRIPT.format(target=json.dumps(str(seen))))
    _enable(monkeypatch)
    hooks.register(events.PRE_TOOL_USE, "ops_observe", handler=HANDLER_COMMAND, command=_command(script))

    assert await hooks.before_tool_use(task=_task(), tool_call=_call("read_file")) is None

    payload = json.loads(seen.read_text(encoding="utf-8"))
    assert payload["event"] == events.PRE_TOOL_USE
    assert payload["tool_name"] == "read_file"
    assert payload["tenant_id"] == "t_hookcmd"
    assert payload["session_id"] == "s_hookcmd"


# ── ④ fail-open + 留痕（超时） ───────────────────────────────────────────────


async def test_timeout_fails_open_and_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(tmp_path, "import time\ntime.sleep(30)\n")
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "hooks_timeout_seconds", 1)
    hooks.register(events.PRE_TOOL_USE, "ops_slow", handler=HANDLER_COMMAND, command=_command(script))

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None

    assert _hook_entries(tmp_path)[-1]["outcome"] == "timeout"
    assert _exec_entries(tmp_path)[-1]["outcome"] == "timeout"


async def test_large_output_is_truncated_and_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """输出超限**截断并显式标注**（那是"运维脚本刷屏"的处置），不是 python 形态的"超限即拒"。"""
    script = _script(tmp_path, "print('x' * 600000)\n")
    _enable(monkeypatch)
    hooks.register(events.PRE_TOOL_USE, "ops_noisy", handler=HANDLER_COMMAND, command=_command(script))

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None

    assert _hook_entries(tmp_path)[-1]["truncated"] is True


# ── ⑤ 声明面：`type: command` 端到端 ────────────────────────────────────────


def _doc(tmp_path: Path, *, body: str) -> dict[str, Any]:
    script = _script(tmp_path, body)
    return {
        "hooks": {
            events.PRE_TOOL_USE: [
                {"hooks": [{"type": "command", "name": "ops", "command": _command(script)}]}
            ]
        }
    }


def test_declaration_file_loads_command_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps(_doc(tmp_path, body="import sys\nsys.exit(0)\n")), encoding="utf-8")
    _enable(monkeypatch, config_path=str(target))

    result = hooks_config.load_hooks_config()

    assert result.failed is False
    assert result.loaded == 1
    assert [hook.handler for hook in hooks.registry.all()] == [HANDLER_COMMAND]


def test_declaration_file_command_form_is_skipped_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一份文件在开关关闭时**零效果**（验收①：默认零行为变化）。"""
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps(_doc(tmp_path, body="import sys\nsys.exit(0)\n")), encoding="utf-8")
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()

    assert result.loaded == 0
    assert result.skipped == 1
    assert result.failed is False
    assert hooks.registry.all() == []
    outcomes = [entry["outcome"] for entry in _hook_entries(tmp_path)]
    assert "register_denied" in outcomes, "被拒的条目必须留审计（不是静默跳过）"


def test_declaration_file_missing_command_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = {"hooks": {events.PRE_TOOL_USE: [{"hooks": [{"type": "command", "name": "ops"}]}]}}
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    _enable(monkeypatch, config_path=str(target))

    result = hooks_config.load_hooks_config()

    assert result.loaded == 0 and result.skipped == 1
    assert "config_skipped" in [entry["outcome"] for entry in _hook_entries(tmp_path)]
