"""Skill tools (skill_list / skill_install / skill_generate / skill_discover) 注册到本地工具注册表。

实现对标 Go `internal/skill/tools.go`，使用磁盘 SkillStore 作为后端。

## 多租户 / 用户级隔离 + 租户共享层

- 运行时工具链从 app.tools.context（contextvars，AgentRuntime 注入）读取
  user_id / tenant_id，按与 API 层（app/api/skills.py）相同的规则构造 store：
  查找执行沿 `data/skills/{tenant_id}/{user_id}/` → `data/skills/{tenant_id}/_shared/`
  （租户共享层）→ `data/skills/_shared/`（全局共享）三级，取第一个命中
  （SkillStore.get 内部实现，见 app/skill/store.py）；写（install/generate）默认
  user 私有目录。无身份（未登录 / 系统任务）→ `data/skills/_shared/`。
- 函数签名保持兼容：调用方不传身份时行为与历史一致（共享目录）。
- 身份段非法（路径穿越）时回退共享目录并告警，避免工具调用中断。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from app.skill.manager import SkillType
from app.skill.store import SkillDef, SkillStore
from app.tools.registry import registry

logger = logging.getLogger(__name__)

_skill_root = os.getenv("SKILL_STORE_PATH", os.path.join(".", "data", "skills"))
# 无显式 root → 按身份解析；模块加载时无上下文，即为共享目录（含旧目录迁移）
_store = SkillStore()


def _get_store() -> SkillStore:
    """按当前运行上下文解析身份 store；无身份 → 全局共享目录（模块级 _store）。

    有身份时构造带 tenant/user 的 store：读/执行沿 user → 租户 _shared →
    全局 _shared 查找（skill_run 依赖此找到团队共享技能），写默认 user 私有目录。
    """
    from app.tools.context import get_tenant_id, get_user_id

    uid = get_user_id()
    tid = get_tenant_id()
    if uid or tid:
        try:
            return SkillStore(tenant_id=tid, user_id=uid)
        except ValueError as e:
            # 身份段非法（路径穿越防护失败）→ 回退共享目录，不中断工具调用
            logger.warning(
                "skill store: invalid identity from context, fallback to shared: %s", e
            )
    return _store


async def skill_list() -> dict[str, Any]:
    skills = _get_store().list()
    if not skills:
        return {"output": "No skills installed.", "count": 0, "skills": []}
    lines = [
        f"  - {s.name}: {s.description} (v{s.version}, {s.exec_type})" for s in skills
    ]
    payload = []
    for s in skills:
        item = s.to_dict()
        item["source"] = s.scope  # user/tenant/shared，标记团队共享技能
        payload.append(item)
    return {"output": "\n".join(lines), "count": len(skills), "skills": payload}


async def skill_install(
    url: str = "", file: str = "", inline: str = ""
) -> dict[str, Any]:
    if not url and not file and not inline:
        return {"error": "one of url, file, or inline is required"}

    try:
        if inline:
            if len(inline) > 1_048_576:  # S5: 大小上限 1MB
                return {"error": "inline skill definition too large (max 1MB)"}
            data = json.loads(inline)
        elif file:
            from app.tools.core import _safe_path

            safe_file = _safe_path(file, str(_get_store().root))
            if safe_file.stat().st_size > 1_048_576:
                return {"error": "skill file too large (max 1MB)"}
            data = json.loads(safe_file.read_text(encoding="utf-8"))
        else:
            from app.tools.ssrf import assert_safe_url

            assert_safe_url(url)  # S4: SSRF 防护
            import httpx

            from app.config import settings

            async with httpx.AsyncClient(timeout=settings.http_timeout_default) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                if len(resp.content) > 1_048_576:
                    return {"error": "skill definition too large (max 1MB)"}
                data = resp.json()
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"failed to load skill definition: {e}"}

    # S5: 必填字段与 name 校验（防注入路径/覆盖平台保留名）
    name = data.get("name", "")
    if not name or not isinstance(name, str):
        return {"error": "skill name is required"}
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", name):
        return {"error": "skill name must match [a-z0-9][a-z0-9-]{0,63}"}
    if not data.get("description"):
        return {"error": "skill description is required"}

    exec_cfg = data.get("exec", {})
    skill = SkillDef(
        name=name,
        description=data.get("description", ""),
        version=data.get("version", "0.1.0"),
        author=data.get("author", ""),
        tags=data.get("tags", []),
        exec_type=exec_cfg.get("type", "prompt"),
        source=exec_cfg.get("source", ""),
        parameters=data.get("parameters", []),
    )
    _get_store().save(skill)
    return {
        "output": f"Skill installed: {skill.name} (v{skill.version})\n{json.dumps(skill.to_dict(), ensure_ascii=False, indent=2)}",
        "skill": skill.name,
        "version": skill.version,
    }


async def skill_generate(description: str, install: bool = False) -> dict[str, Any]:
    if not description:
        return {"error": "description is required"}

    name = description.strip().lower().replace(" ", "_")[:32] or "generated_skill"
    skill = SkillDef(
        name=name,
        description=description,
        version="0.1.0",
        exec_type="prompt",
        source=f"Generate a concise prompt-based skill for: {description}",
    )
    result: dict[str, Any] = {
        "output": f"Generated skill definition:\n{json.dumps(skill.to_dict(), ensure_ascii=False, indent=2)}",
        "skill": skill.to_dict(),
        "name": skill.name,
        "type": skill.exec_type,
    }

    if install:
        try:
            _get_store().save(skill)
            result["installed"] = True
            result["output"] = (
                f"Generated and installed skill: {skill.name}\n{json.dumps(skill.to_dict(), ensure_ascii=False, indent=2)}"
            )
        except Exception as e:
            result["install_error"] = str(e)

    return result


async def skill_discover(url: str = "") -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    if url:
        try:
            import httpx

            from app.config import settings

            async with httpx.AsyncClient(timeout=settings.http_timeout_default) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                items = resp.json()
                installed_names = {s.name for s in _get_store().list()}
                for item in items:
                    name = item.get("name", "")
                    results.append(
                        {
                            "name": name,
                            "description": item.get("description", ""),
                            "version": item.get("version", ""),
                            "author": item.get("author", ""),
                            "source": item.get("source", url),
                            "installed": name in installed_names,
                        }
                    )
        except Exception as e:
            return {"error": f"discover remote failed: {e}"}
    else:
        for s in _get_store().list():
            results.append(
                {
                    "name": s.name,
                    "description": s.description,
                    "version": s.version,
                    "author": s.author,
                    "source": s.source,
                    "installed": True,
                }
            )

    lines = [
        f"  [{'✓' if r['installed'] else ' '}] {r['name']}: {r['description']} (v{r['version']})"
        for r in results
    ]
    return {
        "output": f"Available skills ({len(results)})\n" + "\n".join(lines),
        "count": len(results),
        "results": results,
    }


async def skill_run(name: str, params: str | dict = "{}") -> dict[str, Any]:
    """执行一个已安装的技能。

    支持四种 exec 类型：
    - prompt：渲染模板后调用 LLM 生成结果
    - python：渲染代码后在工具沙箱（run_code，AST 静态检查 + tools 命名空间）中执行
    - shell：渲染命令后在 sandbox（逃逸拦截 + 环境清理）中执行
    - http：渲染 URL 后经 SSRF 防护抓取内容
    """
    skill = _get_store().get(name)
    if not skill:
        return {"error": f"Skill not found: {name}"}
    if not skill.enabled:
        return {"error": f"Skill '{name}' is disabled"}

    try:
        param_dict = json.loads(params) if isinstance(params, str) else (params or {})
    except json.JSONDecodeError:
        param_dict = {"input": params}
    if not isinstance(param_dict, dict):
        param_dict = {"input": param_dict}

    try:
        if skill.exec_type == SkillType.PROMPT:
            output = await _run_prompt_skill(skill, param_dict)
        elif skill.exec_type == SkillType.PYTHON_SCRIPT:
            output = await _run_python_skill(skill, param_dict)
        elif skill.exec_type == SkillType.SHELL_COMMAND:
            output = await _run_shell_skill(skill, param_dict)
        elif skill.exec_type == SkillType.HTTP_REQUEST:
            output = await _run_http_skill(skill, param_dict)
        else:
            return {"error": f"Unsupported skill type: {skill.exec_type}"}
    except Exception as e:  # noqa: BLE001 — 执行异常以结构化结果返回，便于模型自纠
        return {"error": f"skill execution failed: {e}"}

    return {"output": output, "skill": name, "exec_type": skill.exec_type}


def _render_template(
    source: str, params: dict[str, Any], parameters: list[dict[str, Any]]
) -> str:
    """渲染模板：先注入参数默认值，再替换 {key} 占位符。"""
    merged: dict[str, Any] = {}
    for p in parameters or []:
        if "default" in p:
            merged[p.get("name", "")] = p.get("default")
    merged.update(params or {})
    rendered = source
    for key, value in merged.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


async def _run_prompt_skill(skill: SkillDef, params: dict[str, Any]) -> str:
    """prompt 技能：渲染模板 → LLM 生成。"""
    from app.main import get_gateway

    try:
        gateway = await get_gateway()
    except Exception:  # noqa: BLE001 — 引擎未初始化时明确报错
        return "error: LLM gateway not initialized"
    if gateway is None:
        return "error: LLM gateway not initialized"

    prompt = _render_template(skill.source, params, skill.parameters)
    messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
    text = ""
    async for chunk in gateway.chat_stream(messages=messages):
        if chunk.content:
            text += chunk.content
    return text or "(empty response)"


async def _run_python_skill(skill: SkillDef, params: dict[str, Any]) -> str:
    """python 技能：渲染代码后在工具沙箱中执行（复用 run_code 的 AST 检查与 tools 命名空间）。"""
    from app.tools.run_code import run_code

    code = _render_template(skill.source, params, skill.parameters)
    result = await run_code(code, description=f"skill:{skill.name}")
    if result.get("isError"):
        return f"error: {result.get('message', 'execution failed')}"
    value = result.get("result", result.get("output", ""))
    if value is None or value == "":
        logs = (result.get("logs") or "").strip()
        return logs or "(no output)"
    return str(value)


async def _run_shell_skill(skill: SkillDef, params: dict[str, Any]) -> str:
    """shell 技能：渲染命令后在 sandbox 中执行（逃逸拦截 + 环境清理）。"""
    from app.tools.sandbox import run_in_sandbox

    command = _render_template(skill.source, params, skill.parameters)
    result = await run_in_sandbox(command)
    if result.get("error"):
        return f"error: {result['error']}"
    stdout = (result.get("stdout") or "").strip()
    stderr = (result.get("stderr") or "").strip()
    parts = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr] {stderr}")
    return "\n".join(parts) or "(empty output)"


async def _run_http_skill(skill: SkillDef, params: dict[str, Any]) -> str:
    """http 技能：渲染 URL 后经 SSRF 防护抓取内容。"""
    from app.config import settings
    from app.tools.ssrf import fetch_url_safe

    url = _render_template(skill.source, params, skill.parameters).strip()
    if not url.startswith(("http://", "https://")):
        return f"error: invalid url: {url}"
    import httpx

    async with httpx.AsyncClient(timeout=settings.http_timeout_web) as client:
        resp = await fetch_url_safe(client, url)
        text = resp.text[:20000]
    return f"[HTTP {resp.status_code}]\n{text}"


registry.register(
    name="skill_list",
    description="List all installed skills with their descriptions and versions.",
    parameters={"type": "object", "properties": {}},
    handler=skill_list,
)

registry.register(
    name="skill_install",
    description="Install a skill from a URL, local file path, or inline JSON definition.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "default": ""},
            "file": {"type": "string", "default": ""},
            "inline": {"type": "string", "default": ""},
        },
    },
    handler=skill_install,
)

registry.register(
    name="skill_generate",
    description="Generate a new skill from a natural language description and optionally install it.",
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "install": {"type": "boolean", "default": False},
        },
        "required": ["description"],
    },
    handler=skill_generate,
)

registry.register(
    name="skill_discover",
    description="Discover available skills from local directory or remote index.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "default": ""},
        },
    },
    handler=skill_discover,
)

registry.register(
    name="skill_run",
    description="Execute an installed skill: render its prompt template with provided parameters and return the result.",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the installed skill"},
            "params": {
                "type": "string",
                "default": "{}",
                "description": "JSON string of parameters to inject into the skill template",
            },
        },
        "required": ["name"],
    },
    handler=skill_run,
)
