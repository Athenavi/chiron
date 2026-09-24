#!/usr/bin/env python
"""工具健康体检 —— 67 个内置工具的端到端可用性检查。

## 为什么不"逐个全调一遍"

那既不安全也不可行：
- `shell_exec` / `write_file` / `git_commit` / `forget` 是**破坏性**的；
- `browser_*` 需要 RPA hub 连着 Go 网关；`image_generate` / `speech_to_text` 需要真实 API key；
- `ask_user` 需要用户交互；`recall` / `kb_search` 需要 DB 与租户数据。

硬调它们只会得到一堆"环境不可用"，**证明不了工具本身有没有问题**。

## 真正有判别力的检查

**`parameters` schema 与 handler 签名是否一致** —— 这是端到端可用性的本质：

| 不一致 | 后果 |
| --- | --- |
| schema 声明了 handler **不接受**的参数（且无 `**kwargs`） | LLM 一旦传它 ⇒ `TypeError` ⇒ **工具永久不可用** |
| handler 有**必填**参数但 schema 没声明 | LLM 无从知道要传 ⇒ **工具永久不可用** |
| schema 声明了**必填**参数但 handler 有默认值 | 无功能性问题，但会让模型多问一次 |

## 四层检查

1. **注册完整性**：名字 / 描述 / parameters 齐备；
2. **schema 合法性**：是合法的 JSON Schema object；
3. **签名一致性**：上面那张表；
4. **安全分级清单**：只读（可安全实调）/ 破坏性 / 需外部服务 / 需交互。

用法（从仓库根或任意目录）：
    python scripts/tool_health_check.py
    python scripts/tool_health_check.py --call-readonly   # 额外实际调用只读工具
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python-engine"))

# 安全分级：明确哪些**不能**随便调
READONLY = {
    "read_file",
    "mode_list",
    "skill_list",
    "kb_list",
    "graph_templates",
    "agent_list",
    "agent_session_list",
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
    "glob_files",
    "grep_files",
    "browser_tab_list",
    "browser_get_state",
    "job_output",
}
DESTRUCTIVE = {
    "write_file",
    "edit_file",
    "shell_exec",
    "execute_python",
    "run_code",
    "persistent_shell",
    "git_commit",
    "forget",
    "remember",
    "media_create",
    "skill_install",
    "skill_generate",
    "mode_edit",
    "job_kill",
    "forget_all",
}
NEEDS_EXTERNAL = {
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_read",
    "browser_screenshot",
    "browser_scroll",
    "browser_tab_create",
    "browser_tab_switch",
    "browser_tab_close",
    "image_generate",
    "vision_analyze",
    "speech_to_text",
    "text_to_speech",
    "file_analyzer",
    "read_image",
    "kb_search",
    "rag_query",
    "memory_search",
    "recall",
    "web_fetch",
    "search_files",
    "agent_dispatch",
    "code_agent",
    "subagent",
    "read_subagent_result",
    "run_in_background",
    "workflow_run",
    "workflow_status",
    "graph_run",
    "graph_create",
    "prd_generate",
    "tech_design",
    "task_decompose",
    "requirement_validate",
}
NEEDS_INTERACTION = {"ask_user"}


def schema_param_names(schema: dict) -> tuple[set[str], set[str]]:
    """返回 (声明的属性名, 声明的必填名)。"""
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    return set(props.keys()), set(required)


def handler_signature(handler) -> tuple[set[str], set[str], bool]:
    """返回 (可接受的具名参数, 无默认值的必填参数, 是否有 **kwargs)。"""
    sig = inspect.signature(handler)
    accepts: set[str] = set()
    required: set[str] = set()
    var_kw = False
    for pname, p in sig.parameters.items():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            var_kw = True
            continue
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        accepts.add(pname)
        if p.default is inspect.Parameter.empty:
            required.add(pname)
    return accepts, required, var_kw


def classify(name: str) -> str:
    if name in NEEDS_INTERACTION:
        return "interaction"
    if name in DESTRUCTIVE:
        return "destructive"
    if name in NEEDS_EXTERNAL:
        return "external"
    if name in READONLY:
        return "readonly"
    return "unknown"


async def call_readonly(names: list[str]) -> None:
    """实际调用只读工具（尽量给出安全参数），观察是否抛错。"""
    from app.tools.registry import registry

    # 沙箱根：fs_guard 会拒绝沙箱外的路径，实调参数必须落在沙箱内，
    # 否则得到的全是 "path escapes sandbox" —— 那是拒绝生效，不是工具坏了。
    try:
        from app.tools.sandbox import workspace_dir

        ws = workspace_dir()
        ws = str(ws() if callable(ws) else ws)
    except Exception:  # noqa: BLE001
        ws = "."
    print(f"  （沙箱根: {ws}）")

    # ⚠️ 参数名必须与 handler 完全一致：grep_files 的第一个参数是 query（不是 pattern）。
    # 之前这里的笔误一度被误判为工具缺陷 —— 实调的价值在于能证伪，也在于能证伪自己。
    safe_args: dict[str, dict] = {
        "mode_list": {},
        "skill_list": {},
        "kb_list": {},
        "graph_templates": {},
        "agent_list": {},
        "agent_session_list": {},
        "git_status": {"root": "."},
        "git_branch": {"root": "."},
        "git_log": {"root": ".", "limit": 1},
        "glob_files": {"pattern": "README*", "root": "."},
        "grep_files": {"query": "def ", "root": "."},
        "read_file": {"path": "README.md"},
        "search_files": {"root": "."},
        "job_output": {"job_id": "__nonexistent__"},
        "browser_tab_list": {},
        "browser_get_state": {},
    }
    print()
    print("── 只读工具实调 ──")
    for name in names:
        if name not in READONLY:
            continue
        args = safe_args.get(name)
        if args is None:
            print(f"  SKIP  {name:26} （无安全参数样例）")
            continue
        try:
            result = await registry.execute(name, args)
            err = result.get("error") if isinstance(result, dict) else None
            # 业务层面的"没找到/未配置"是正常答复，只把异常/未实现视为问题
            if err and ("not implemented" in str(err).lower() or "traceback" in str(err).lower()):
                print(f"  FAIL  {name:26} {str(err)[:90]}")
            else:
                head = json.dumps(result, ensure_ascii=False)[:90]
                print(f"  OK    {name:26} {head}")
        except Exception as e:  # noqa: BLE001 — 体检就是要抓全部异常
            print(f"  FAIL  {name:26} {type(e).__name__}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--call-readonly", action="store_true", help="额外实际调用只读工具")
    args = parser.parse_args()

    import app.tools  # noqa: F401
    from app.tools.registry import registry
    from app.agent import modes

    names = sorted(registry.list_names())
    print(f"注册工具总数: {len(names)}")

    problems: list[str] = []
    buckets = {"readonly": 0, "destructive": 0, "external": 0, "interaction": 0, "unknown": 0}

    for name in names:
        tool = registry.get(name)
        assert tool is not None
        kind = classify(name)
        buckets[kind] += 1

        # 1) 注册完整性
        if not tool.description or len(tool.description.strip()) < 8:
            problems.append(f"{name}: description 缺失或过短")
        if not isinstance(tool.parameters, dict) or not tool.parameters:
            problems.append(f"{name}: parameters 缺失")
            continue

        # 2) schema 合法性
        if tool.parameters.get("type") != "object":
            problems.append(f"{name}: parameters.type 应为 'object'（实际 {tool.parameters.get('type')!r}）")
        declared, declared_required = schema_param_names(tool.parameters)

        # 3) 签名一致性
        accepts, sig_required, var_kw = handler_signature(tool.handler)
        if not var_kw:
            extra = declared - accepts
            if extra:
                problems.append(
                    f"{name}: schema 声明了 handler 不接受的参数 {sorted(extra)} ⇒ 调用必 TypeError"
                )
        missing = sig_required - declared
        if missing:
            problems.append(
                f"{name}: handler 必填参数 {sorted(missing)} 未在 schema 声明 ⇒ LLM 无从传入，必然失败"
            )
        if declared_required - accepts and not var_kw:
            problems.append(f"{name}: schema 的 required 含 handler 不接受的参数")

    print(f"分级: 只读 {buckets['readonly']} / 破坏性 {buckets['destructive']} / "
          f"需外部 {buckets['external']} / 需交互 {buckets['interaction']} / 未分类 {buckets['unknown']}")

    # 模式工具集一致性
    print()
    print("── 模式工具集 ──")
    reg = set(names)
    for label, const in [
        ("CORE", modes.CORE_TOOL_NAMES),
        ("MINIMAL", modes.MINIMAL_TOOL_NAMES),
        ("PTC_EXTRA", modes.PTC_EXTRA_TOOLS),
        ("CREATIVE_EXTRA", modes.CREATIVE_EXTRA_TOOLS),
    ]:
        miss = sorted(set(const) - reg)
        print(f"  {label:15} {len(const):3} 个  " + ("OK" if not miss else f"缺失 {miss}"))
        if miss:
            problems.append(f"{label} 引用了未注册的工具: {miss}")

    print()
    if problems:
        print(f"── 发现 {len(problems)} 个问题 ──")
        for p in problems:
            print(f"  ✗ {p}")
    else:
        print("── 未发现问题 ──")

    if args.call_readonly:
        asyncio.run(call_readonly(names))

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
