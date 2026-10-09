"""批 G+（钩子协议批次 1）：**部署级声明面** `hooks.json`。

设计见 `docs/hook-protocol-design.md` §4.2。三条纪律：

1. **默认零行为变化**：`settings.hooks_config_path` 为空 ⇒ 不读文件、不注册、不执行；
2. **失败显式**：文件不可读 / 非法 JSON / 结构不对 / 超限 ⇒ error 日志 + 审计，且
   **不注册任何 hook**（引擎照常启动）—— 不静默降级；
3. **只接受部署级声明**：文件里的条目一律 `OWNER_DEPLOYMENT`；显式写 `owner: user`
   的条目被**拒绝**（多租户下"租户可写的文件"等于把可执行面交给租户，与 §6 冲突）。

格式（Claude Code 兼容子集）::

    {"hooks": {"PreToolUse": [
        {"matcher": "shell_exec",
         "hooks": [{"type": "python", "name": "deny_rm", "code": "def main(input): ..."}]}
    ]}}

`matcher` 是**锚定**正则（`re.fullmatch` 语义），**只对工具类事件合法**（`events.MATCHER_EVENTS`）；
缺失 / 空 ⇒ 该事件的所有调用都跑。

`type` 接受三种形态：`python`（沙箱子进程）· **`command`（批次 2b：运维声明的本地命令）**·
**`webhook`（批次 3：出站 POST 到运维声明的 URL）**。后两者各自有一道闸（独立开关**默认关** +
目标 allowlist **默认空 = fail-closed**），不满足的条目在 `manager.register` 处被拒并留审计 ——
**配了却不生效是最难查的一类问题**，所以宁可当场拒（见 `docs/hook-protocol-design.md` §4.3）：

* `command`：`hooks_allow_commands` + `hooks_command_allowlist`（可执行文件，裸名或绝对路径）；
* `webhook`：`hooks_allow_webhooks` + `hooks_webhook_allowlist`（host 名；scheme/端口/IP 段由
  `app/tools/ssrf.py` 在**执行时**判）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.hooks import audit, events
from app.hooks.manager import (
    HANDLER_COMMAND,
    HANDLER_PYTHON,
    HANDLER_WEBHOOK,
    OWNER_DEPLOYMENT,
    HookManager,
    hooks,
)

logger = logging.getLogger(__name__)

#: 声明文件体积上限（1 MiB）—— 防"读一个巨大的文件"把引擎内存吃掉。
MAX_FILE_BYTES = 1 << 20
#: 一次加载允许注册的 hook 总数上限（防声明文件把每个工具调用拖成 N 次子进程）。
MAX_HOOKS_PER_LOAD = 64
#: 单个 hook 的源码上限（64 KiB）。
MAX_CODE_BYTES = 64 << 10
#: `command` 形态的命令文本上限（64 KiB，与 `code` 同量级）—— 命令本就该是短的一行。
MAX_COMMAND_BYTES = 64 << 10
#: `webhook` 形态的目标 URL 长度上限（字符）。
MAX_URL_CHARS = 2048
#: `matcher` 字符串长度上限。
MAX_MATCHER_CHARS = 256
#: 目前支持的 handler 形态（批次 2b 加 `command`、批次 3 加 `webhook`）。
SUPPORTED_TYPES: frozenset[str] = frozenset({HANDLER_PYTHON, HANDLER_COMMAND, HANDLER_WEBHOOK})
#: 审计里代表"这不是某个事件、而是声明面本身"的伪事件名（与启动告警同款用法）。
CONFIG_EVENT = "*"
CONFIG_HOOK = "<config>"


@dataclass(frozen=True)
class LoadResult:
    """一次声明文件加载的结果（供启动日志与测试断言）。"""

    loaded: int = 0
    skipped: int = 0
    failed: bool = False
    reason: str = ""


def _audit(outcome: str, reason: str) -> None:
    audit.record_hook(
        event=CONFIG_EVENT,
        hook=CONFIG_HOOK,
        owner=OWNER_DEPLOYMENT,
        outcome=outcome,
        reason=reason,
    )


def _fail(reason: str) -> LoadResult:
    """加载失败：**显式**记 error + 审计，且调用方保证什么都不注册。"""
    logger.error("hooks config rejected: %s", reason)
    _audit("config_failed", reason)
    return LoadResult(failed=True, reason=reason)


def _skip(reason: str) -> None:
    """跳过单个条目 / 形态：告警 + 审计，其余条目照常加载。"""
    logger.warning("hooks config entry skipped: %s", reason)
    _audit("config_skipped", reason)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _register_group(
    manager: HookManager,
    event: str,
    entry: Any,
    *,
    budget: int,
) -> tuple[int, int]:
    """注册一个 `{matcher, hooks[]}` 组，返回 `(loaded, skipped)`。"""
    if not isinstance(entry, dict):
        _skip(f"{event}: entry is not an object")
        return 0, 1
    raw_matcher = _text(entry.get("matcher"))
    if len(raw_matcher) > MAX_MATCHER_CHARS:
        _skip(f"{event}: matcher exceeds {MAX_MATCHER_CHARS} chars")
        return 0, 1
    if raw_matcher and not events.matcher_applies(event):
        _skip(f"{event}: matcher is only valid on tool events")
        return 0, 1
    if raw_matcher:
        try:
            re.compile(raw_matcher)
        except re.error as exc:
            _skip(f"{event}: invalid matcher regex: {exc}")
            return 0, 1

    items = entry.get("hooks")
    if not isinstance(items, list) or not items:
        _skip(f"{event}: missing non-empty 'hooks' array")
        return 0, 1

    loaded = 0
    skipped = 0
    for index, item in enumerate(items):
        if loaded >= budget:
            _skip(f"{event}: hook budget ({MAX_HOOKS_PER_LOAD}) reached; remaining entries ignored")
            skipped += len(items) - index
            break
        if not isinstance(item, dict):
            _skip(f"{event}[{index}]: hook is not an object")
            skipped += 1
            continue
        kind = _text(item.get("type")) or HANDLER_PYTHON
        if kind not in SUPPORTED_TYPES:
            supported = ", ".join(sorted(SUPPORTED_TYPES))
            _skip(f"{event}[{index}]: unsupported handler type {kind!r} (supported: {supported})")
            skipped += 1
            continue
        declared_owner = _text(item.get("owner"))
        if declared_owner and declared_owner != OWNER_DEPLOYMENT:
            _skip(f"{event}[{index}]: declaration files may not register {declared_owner!r} hooks")
            skipped += 1
            continue
        name = _text(item.get("name")) or f"{event}#{index}"
        if kind == HANDLER_COMMAND:
            command = _text(item.get("command"))
            if not command:
                _skip(f"{event}[{index}]: missing 'command'")
                skipped += 1
                continue
            if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
                _skip(f"{event}[{index}]: command exceeds {MAX_COMMAND_BYTES} bytes")
                skipped += 1
                continue
            # 独立开关 + 部署自备 allowlist 的判定**只在 `manager.register` 一处** ——
            # 文件入口与进程内入口共用同一条闸，不会"一边拦一边放"。
            registered = manager.register(
                event,
                name,
                owner=OWNER_DEPLOYMENT,
                matcher=raw_matcher,
                handler=HANDLER_COMMAND,
                command=command,
            )
        elif kind == HANDLER_WEBHOOK:
            url = _text(item.get("url"))
            if not url:
                _skip(f"{event}[{index}]: missing 'url'")
                skipped += 1
                continue
            if len(url) > MAX_URL_CHARS:
                _skip(f"{event}[{index}]: url exceeds {MAX_URL_CHARS} chars")
                skipped += 1
                continue
            registered = manager.register(
                event,
                name,
                owner=OWNER_DEPLOYMENT,
                matcher=raw_matcher,
                handler=HANDLER_WEBHOOK,
                url=url,
            )
        else:
            code = _text(item.get("code"))
            if not code:
                _skip(f"{event}[{index}]: missing 'code'")
                skipped += 1
                continue
            if len(code.encode("utf-8")) > MAX_CODE_BYTES:
                _skip(f"{event}[{index}]: code exceeds {MAX_CODE_BYTES} bytes")
                skipped += 1
                continue
            registered = manager.register(
                event, name, code, owner=OWNER_DEPLOYMENT, matcher=raw_matcher
            )
        if registered:
            loaded += 1
        else:
            skipped += 1
    return loaded, skipped


def load_hooks_config(
    path: str | None = None, *, manager: HookManager | None = None
) -> LoadResult:
    """从声明文件加载部署级 hook（**永不抛异常**）。

    Args:
        path: 声明文件路径；`None` ⇒ 用 `settings.hooks_config_path`（空 = 空操作）。
        manager: 目标注册表；默认进程内单例 `hooks`（测试可注入）。

    Returns:
        `LoadResult`。失败时 `failed=True` 且**没有任何 hook 被注册**。
    """
    target = settings.hooks_config_path if path is None else path
    if not target:
        return LoadResult()
    sink = hooks if manager is None else manager

    source = Path(target)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        return _fail(f"cannot read {target}: {exc}")
    if len(raw) > MAX_FILE_BYTES:
        return _fail(f"{target} exceeds {MAX_FILE_BYTES} bytes")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _fail(f"{target} is not valid UTF-8 JSON: {exc}")
    if not isinstance(document, dict):
        return _fail(f"{target}: top level must be an object")
    group = document.get("hooks")
    if not isinstance(group, dict):
        return _fail(f"{target}: missing 'hooks' object")

    loaded = 0
    skipped = 0
    for event_name, entries in group.items():
        if not isinstance(event_name, str) or event_name not in events.ALL_EVENTS:
            _skip(f"unknown event {event_name!r} (ignored, config stays valid)")
            skipped += 1
            continue
        if not isinstance(entries, list):
            _skip(f"{event_name}: value must be an array")
            skipped += 1
            continue
        for entry in entries:
            group_loaded, group_skipped = _register_group(
                sink, event_name, entry, budget=MAX_HOOKS_PER_LOAD - loaded
            )
            loaded += group_loaded
            skipped += group_skipped

    logger.info(
        "hooks config loaded from %s: %d registered, %d skipped", target, loaded, skipped
    )
    _audit("config_loaded", f"{target}: loaded={loaded} skipped={skipped}")
    return LoadResult(loaded=loaded, skipped=skipped)
