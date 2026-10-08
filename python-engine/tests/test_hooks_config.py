"""批 G+（钩子协议批次 1）：部署级声明面 `hooks.json` 回归。

对应 `docs/hook-protocol-design.md` §7 的验收项：
① 默认零行为变化；② 声明面真的生效；③ 只能收紧（matcher 只做筛选，不放宽）；
⑥ 解析失败不静默且不注册。

另覆盖：matcher **锚定**语义（`file` 不该命中 `read_file` —— Reasonix 2.x 文档里的同款教训）、
未知事件忽略、不支持的 handler 形态跳过、非工具事件带 matcher 拒绝、文件超限拒绝、
`owner: user` 拒绝、预算上限。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.hooks import OWNER_DEPLOYMENT, events, hooks
from app.hooks import config as hooks_config

DENY_HOOK = 'def main(input):\n    return {"decision": "deny", "reason": "blocked by test policy"}\n'
NOOP_HOOK = "def main(input):\n    return {}\n"


@pytest.fixture(autouse=True)
def _hooks_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例：审计落盘指向 tmp、开关复位、清空注册表、声明路径清空。"""
    from app.hooks import audit

    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(settings, "hooks_enabled", False)
    monkeypatch.setattr(settings, "hooks_allow_user_defined", False)
    monkeypatch.setattr(settings, "hooks_timeout_seconds", 5)
    monkeypatch.setattr(settings, "hooks_config_path", "")
    hooks.clear()
    yield
    hooks.clear()


def _task(**overrides: Any) -> SimpleNamespace:
    base = {"tenant_id": "t1", "user_id": "u1", "session_id": "s1"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _call(name: str = "shell_exec", arguments: str = '{"cmd": "ls"}') -> dict[str, Any]:
    return {"id": "call_1", "name": name, "arguments": arguments}


def _entries(root: Path) -> list[dict[str, Any]]:
    path = root / "hooks_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write(tmp_path: Path, document: Any, name: str = "hooks.json") -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return target


def _doc(event: str, *, matcher: str = "", code: str = DENY_HOOK, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"hooks": [{"type": "python", "name": "h", "code": code}]}
    if matcher:
        entry["matcher"] = matcher
    entry["hooks"][0].update(extra)
    return {"hooks": {event: [entry]}}


# ── ① 默认零行为变化 ─────────────────────────────────────────────────


def test_empty_path_is_a_noop(tmp_path: Path) -> None:
    """默认路径为空：不读文件、不注册、无审计。"""
    result = hooks_config.load_hooks_config()
    assert result == hooks_config.LoadResult()
    assert hooks.registry.all() == []
    assert _entries(tmp_path) == []


async def test_loaded_hooks_do_not_run_while_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """声明文件加载成功，但总开关仍关 ⇒ 零执行、零执行审计。"""
    target = _write(tmp_path, _doc(events.PRE_TOOL_USE))
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.loaded == 1 and not result.failed

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert [entry["outcome"] for entry in _entries(tmp_path)] == ["config_loaded"]


# ── ② 声明面真的生效 ─────────────────────────────────────────────────


async def test_declared_hook_blocks_matching_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write(tmp_path, _doc(events.PRE_TOOL_USE, matcher="shell_exec"))
    monkeypatch.setattr(settings, "hooks_config_path", str(target))
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks_config.load_hooks_config()

    blocked = await hooks.before_tool_use(task=_task(), tool_call=_call("shell_exec"))
    assert blocked is not None, "命中的 hook 必须能阻断"
    assert _entries(tmp_path)[-1]["blocked"] is True


def test_declared_hooks_are_deployment_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """部署级声明不受 `hooks_allow_user_defined=false` 影响（它约束的是租户来源）。"""
    target = _write(tmp_path, _doc(events.STOP, code=NOOP_HOOK))
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.loaded == 1
    registered = hooks.registry.all()
    assert len(registered) == 1
    assert registered[0].owner == OWNER_DEPLOYMENT


# ── matcher：锚定语义（`file` 不该命中 `read_file`） ─────────────────


async def test_matcher_is_anchored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write(tmp_path, _doc(events.PRE_TOOL_USE, matcher="file"))
    monkeypatch.setattr(settings, "hooks_config_path", str(target))
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks_config.load_hooks_config()

    # 未锚定就会误伤：`read_file` 不该被 `file` 命中
    assert await hooks.before_tool_use(task=_task(), tool_call=_call("read_file")) is None
    # 精确同名才命中
    assert await hooks.before_tool_use(task=_task(), tool_call=_call("file")) is not None


async def test_matcher_supports_explicit_patterns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write(tmp_path, _doc(events.PRE_TOOL_USE, matcher=".*_file"))
    monkeypatch.setattr(settings, "hooks_config_path", str(target))
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks_config.load_hooks_config()

    assert await hooks.before_tool_use(task=_task(), tool_call=_call("read_file")) is not None
    assert await hooks.before_tool_use(task=_task(), tool_call=_call("shell_exec")) is None


async def test_absent_matcher_matches_every_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write(tmp_path, _doc(events.PRE_TOOL_USE))
    monkeypatch.setattr(settings, "hooks_config_path", str(target))
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks_config.load_hooks_config()

    for tool in ("shell_exec", "read_file", "anything"):
        assert await hooks.before_tool_use(task=_task(), tool_call=_call(tool)) is not None


# ── ⑥ 解析失败：不静默、不注册 ───────────────────────────────────────


def test_missing_file_is_a_failure_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hooks_config_path", str(tmp_path / "nope.json"))

    result = hooks_config.load_hooks_config()
    assert result.failed is True
    assert "cannot read" in result.reason
    assert hooks.registry.all() == []
    assert _entries(tmp_path)[-1]["outcome"] == "config_failed"


def test_bad_json_registers_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "hooks.json"
    target.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.failed is True
    assert hooks.registry.all() == []
    assert _entries(tmp_path)[-1]["outcome"] == "config_failed"


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"nope": {}},
        {"hooks": []},
    ],
)
def test_structurally_wrong_documents_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: Any
) -> None:
    target = _write(tmp_path, document)
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.failed is True
    assert hooks.registry.all() == []


def test_oversized_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks_config, "MAX_FILE_BYTES", 16)
    target = _write(tmp_path, _doc(events.PRE_TOOL_USE))
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.failed is True
    assert "exceeds" in result.reason
    assert hooks.registry.all() == []


# ── 单个条目被跳过：其余照常加载（配置不整体失效） ───────────────────


def test_unknown_event_is_skipped_but_others_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = {
        "hooks": {
            "NotARealEvent": [{"hooks": [{"type": "python", "code": NOOP_HOOK}]}],
            events.STOP: [{"hooks": [{"type": "python", "name": "ok", "code": NOOP_HOOK}]}],
        }
    }
    target = _write(tmp_path, document)
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.loaded == 1
    assert result.skipped == 1
    assert result.failed is False
    assert [hook.name for hook in hooks.registry.all()] == ["ok"]


def test_unsupported_handler_type_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """批次 1 只支持 `python`；`command` / `webhook` 在批次 2 / 3 落地，现在跳过并告警。"""
    document = _doc(events.PRE_TOOL_USE, code=NOOP_HOOK)
    document["hooks"][events.PRE_TOOL_USE][0]["hooks"][0]["type"] = "command"

    target = _write(tmp_path, document)
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.loaded == 0
    assert result.skipped == 1
    assert hooks.registry.all() == []
    outcomes = [entry["outcome"] for entry in _entries(tmp_path)]
    assert "config_skipped" in outcomes, "跳过的条目必须留审计"
    assert outcomes[-1] == "config_loaded", "整体加载成功（其余条目可用）"


def test_matcher_on_non_tool_event_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write(tmp_path, _doc(events.STOP, matcher="anything", code=NOOP_HOOK))
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.loaded == 0 and result.skipped == 1
    assert hooks.registry.all() == []


def test_invalid_regex_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write(tmp_path, _doc(events.PRE_TOOL_USE, matcher="(unclosed", code=NOOP_HOOK))
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.loaded == 0 and result.skipped == 1
    assert hooks.registry.all() == []


def test_owner_user_in_file_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件里显式写 `owner: user` 一律拒绝 —— 文件是部署级通道，不能声明租户级 hook。"""
    document = _doc(events.PRE_TOOL_USE, code=NOOP_HOOK, owner="user")
    target = _write(tmp_path, document)
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.loaded == 0 and result.skipped == 1
    assert hooks.registry.all() == []


def test_hook_budget_caps_registrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks_config, "MAX_HOOKS_PER_LOAD", 2)
    items = [
        {"type": "python", "name": f"h{i}", "code": NOOP_HOOK} for i in range(4)
    ]
    target = _write(tmp_path, {"hooks": {events.STOP: [{"hooks": items}]}})
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.loaded == 2
    assert result.skipped == 2
    assert len(hooks.registry.all()) == 2


def test_missing_code_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write(tmp_path, {"hooks": {events.STOP: [{"hooks": [{"type": "python"}]}]}})
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()
    assert result.loaded == 0 and result.skipped == 1
    assert hooks.registry.all() == []


def test_explicit_path_argument_overrides_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 path 优先于 `settings.hooks_config_path`（供测试与嵌入方使用）。"""
    target = _write(tmp_path, _doc(events.STOP, code=NOOP_HOOK))
    monkeypatch.setattr(settings, "hooks_config_path", str(tmp_path / "ignored.json"))

    result = hooks_config.load_hooks_config(str(target))
    assert result.loaded == 1
