"""Skill API endpoints — CRUD + discover, backed by SkillStore.

## 多租户 / 用户级隔离 + 租户共享层（四级目录解析）

- 所有 handler 从 query 参数读取 `user_id` / `tenant_id`（由网关在代理时注入），
  每个请求构造带身份的 `SkillStore`（根目录解析见 app/skill/store.py）：
  读查找沿 `data/skills/{tenant_id}/{user_id}/` → `data/skills/{tenant_id}/_shared/`
  （租户共享层）→ `data/skills/_shared/`（全局共享）三级合并；两者都缺失
  （未登录 / 系统任务）时回退 `data/skills/_shared/`（首次访问自动迁移旧全局目录）。
- 写目标：默认 user 私有目录；`?scope=tenant` 时写租户共享层（需 tenant_id），
  `?scope=private` / 缺省时写 user 私有目录。
- `GET /v1/skills` 返回结构每个技能含 `source` 字段（user/tenant/shared），
  前端可据此显示“团队共享”徽标（source == 'tenant'）。
- 身份段非法（如 `../` 路径穿越）→ 400，拒绝构造 store。
- `run` handler 会把请求身份写入 tool context（app.tools.context），保证
  tools/skill.py 的 `skill_run` 执行链（含沙箱工具）落在同一身份目录下。
- 删除：`DELETE /v1/skills/{name}` 默认删 user 私有目录中的技能；带 `?scope=tenant` 时
  删**租户共享层**（团队资产，网关侧要求技能管理权限 `PermMarketManage`）。目标层里没有
  该技能时返回 404（不会"删了个空"却回 200）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.skill.store import SkillDef, SkillStore

router = APIRouter(tags=["skills"])


def _valid_scope(scope: str = "") -> str:
    """校验安装/注册目标 scope：空 / 'private' → user 私有；'tenant' → 租户共享。"""
    if scope in ("", "private"):
        return ""
    if scope == "tenant":
        return "tenant"
    raise HTTPException(
        status_code=400, detail="invalid scope: must be 'tenant' or 'private'"
    )


def _store_for(user_id: str = "", tenant_id: str = "", scope: str = "") -> SkillStore:
    """按请求身份构造 store；身份非法时 400（防路径穿越）。"""
    try:
        return SkillStore(tenant_id=tenant_id, user_id=user_id, scope=scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid identity: {e}") from e


def _skill_payload(s: SkillDef) -> dict[str, Any]:
    """序列化技能并附带 source 标记（user/tenant/shared），供前端展示共享徽标。"""
    payload = s.to_dict()
    payload["source"] = s.scope
    return payload


@router.get("/v1/skills")
async def list_skills(user_id: str = "", tenant_id: str = "") -> dict[str, Any]:
    """列出全部可用技能：合并 user 目录 + 租户 _shared + 全局 _shared（去重，user 优先）。"""
    store = _store_for(user_id, tenant_id)
    skills = store.list()
    return {
        "skills": [_skill_payload(s) for s in skills],
        "count": len(skills),
    }


class SkillInstallRequest(BaseModel):
    url: str = ""
    file: str = ""
    inline: str = ""


@router.post("/v1/skills/{name}/register")
async def register_skill(
    name: str, user_id: str = "", tenant_id: str = "", scope: str = ""
) -> dict[str, Any]:
    """将能力注册中心的技能注册为本地技能（SkillStore），供对话/Agent 工具链调用。

    前端 SkillMarketCard 调用 POST /v1/skills/{capabilityId}/register；
    能力注册中心查询不到时返回 404，避免假注册。
    scope=tenant 时注册进租户共享层（团队共享），缺省写 user 私有目录。
    """
    from app.core.capabilities import get_registry

    store = _store_for(user_id, tenant_id, _valid_scope(scope))

    cap = await get_registry().get_by_id(name, tenant_id=tenant_id)
    if cap is None:
        raise HTTPException(status_code=404, detail=f"capability not found: {name}")

    existing = [s for s in store.list() if s.name == name]
    if existing:
        return {
            "success": True,
            "skill": name,
            "registered": True,
            "scope": store.write_scope,
        }

    skill = SkillDef(
        name=cap.name or name,
        description=cap.description or f"Skill {name}",
        version="0.1.0",
        exec_type="prompt",
        source=f"Use the {name} capability. {cap.description or ''}".strip(),
    )
    store.save(skill)
    return {
        "success": True,
        "skill": skill.name,
        "registered": True,
        "description": skill.description,
        "scope": store.write_scope,
    }


@router.post("/v1/skills/install")
async def install_skill(
    body: SkillInstallRequest, user_id: str = "", tenant_id: str = "", scope: str = ""
) -> dict[str, Any]:
    """安装技能（url / file / inline）；scope=tenant 时安装进租户共享层。"""
    store = _store_for(user_id, tenant_id, _valid_scope(scope))

    if not body.url and not body.file and not body.inline:
        raise HTTPException(status_code=400, detail="provide url, file, or inline")

    try:
        if body.inline:
            data = json.loads(body.inline)
        elif body.file:
            from app.tools.core import _safe_path

            safe_file = _safe_path(body.file, str(store.root))
            data = json.loads(safe_file.read_text(encoding="utf-8"))
        else:
            import httpx

            from app.config import settings

            async with httpx.AsyncClient(timeout=settings.http_timeout_default) as client:
                resp = await client.get(body.url)
                resp.raise_for_status()
                data = resp.json()
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"failed to load skill definition: {e}"
        ) from e

    exec_cfg = data.get("exec", {})
    skill_name = data.get("name", "")
    if not skill_name:
        raise HTTPException(status_code=400, detail="skill definition missing 'name'")
    skill = SkillDef(
        name=skill_name,
        description=data.get("description", ""),
        version=data.get("version", "0.1.0"),
        author=data.get("author", ""),
        tags=data.get("tags", []),
        exec_type=exec_cfg.get("type", "prompt"),
        source=exec_cfg.get("source", ""),
        parameters=data.get("parameters", []),
    )
    store.save(skill)
    payload = skill.to_dict()
    payload["source"] = store.write_scope
    return {
        "skill": payload,
        "message": f"Skill '{skill.name}' installed",
        "scope": store.write_scope,
    }


class SkillGenerateRequest(BaseModel):
    description: str
    auto_install: bool = False


@router.post("/v1/skills/generate")
async def generate_skill(
    body: SkillGenerateRequest, user_id: str = "", tenant_id: str = "", scope: str = ""
) -> dict[str, Any]:
    """生成技能；auto_install 时保存，scope=tenant 则保存进租户共享层。"""
    store = _store_for(user_id, tenant_id, _valid_scope(scope))

    if not body.description:
        raise HTTPException(status_code=400, detail="description is required")

    name = body.description.strip().lower().replace(" ", "_")[:32] or "generated_skill"
    skill = SkillDef(
        name=name,
        description=body.description,
        version="0.1.0",
        exec_type="prompt",
        source=f"Generate a concise prompt-based skill for: {body.description}",
    )
    result: dict[str, Any] = {"skill": skill.to_dict(), "message": "Skill generated"}

    if body.auto_install:
        store.save(skill)
        result["message"] = f"Skill '{skill.name}' generated and installed"
        result["scope"] = store.write_scope

    return result


@router.delete("/v1/skills/{name}")
async def delete_skill(
    name: str, user_id: str = "", tenant_id: str = "", scope: str = ""
) -> dict[str, str]:
    """删除技能。

    `scope` 决定删**哪一层**（即 store 的写目标，见 `_store_for` 与 `store.write_scope`）：

    * 缺省 / `private`：调用者**私有**目录 —— 任何登录用户都能删自己的技能（既有行为不变）；
    * `tenant`：**租户共享层**（团队资产）。网关已在 `scope=tenant` 时校验技能管理权限
      （`internal/api/skill_handler.go` 的 proxyDelete → `PermMarketManage`）；
      引擎无鉴权能力，网关是唯一可信边界，故此处不重复判权。
    """
    if scope not in ("", "private", "tenant"):
        raise HTTPException(status_code=400, detail=f"invalid scope: {scope}")
    if not re.match(r"^[a-zA-Z0-9_.-]+$", name):
        raise HTTPException(status_code=400, detail="invalid skill name")

    store = _store_for(user_id, tenant_id, scope)

    found = store.get(name)
    if not found:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    # 关键：store.delete 只作用于**写目标目录**。若该技能是从别的层解析出来的
    # （如请求 scope=tenant 但它其实只在全局共享层），删除会是空操作 ——
    # 必须明确 404，而不是回 200 让调用方以为删掉了团队资产。
    if found.scope != store.write_scope:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Skill '{name}' is not in scope '{store.write_scope}' "
                f"(it resolves from '{found.scope}')"
            ),
        )

    store.delete(name)
    return {"message": f"Skill '{name}' deleted", "scope": store.write_scope}


class SkillToggleRequest(BaseModel):
    enabled: bool = True


@router.put("/v1/skills/{name}")
async def toggle_skill(
    name: str, body: SkillToggleRequest, user_id: str = "", tenant_id: str = ""
) -> dict[str, Any]:
    """启用/停用技能（停用后不进 agent 目录、不可运行）。

    命中的技能可能来自租户/全局共享层；回写时落在 user 私有目录形成“本地覆盖”
    （仅影响当前用户，团队共享技能可被成员本地停用）。
    """
    store = _store_for(user_id, tenant_id)

    if not re.match(r"^[a-zA-Z0-9_.-]+$", name):
        raise HTTPException(status_code=400, detail="invalid skill name")
    skill = store.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    skill.enabled = body.enabled
    store.save(skill)
    return {
        "skill": _skill_payload(skill),
        "message": f"Skill '{name}' {'已启用' if body.enabled else '已停用'}",
    }


class SkillRunRequest(BaseModel):
    params: dict[str, Any] = {}


@router.post("/v1/skills/{name}/run")
async def run_skill(
    name: str, body: SkillRunRequest, user_id: str = "", tenant_id: str = ""
) -> dict[str, Any]:
    """运行技能（校验启用状态后调 skill_run）。

    把请求身份写入 tool context 再执行，保证 tools/skill.py 的 skill_run
    （及其内部沙箱/LLM 调用链）落在与校验相同的身份目录下；运行查找同样遵循
    user → 租户 _shared → 全局 _shared 四级规则。
    """
    store = _store_for(user_id, tenant_id)

    if not re.match(r"^[a-zA-Z0-9_.-]+$", name):
        raise HTTPException(status_code=400, detail="invalid skill name")
    skill = store.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    if not skill.enabled:
        raise HTTPException(status_code=400, detail=f"Skill '{name}' 已停用")

    import json as _json

    from app.tools.context import get_all, restore_context, set_tool_context
    from app.tools.skill import skill_run

    snapshot = get_all()
    set_tool_context(user_id=user_id, tenant_id=tenant_id)
    try:
        return await skill_run(name, _json.dumps(body.params))
    finally:
        restore_context(snapshot)


@router.get("/v1/skills/discover")
async def discover_skills(
    url: str = "", user_id: str = "", tenant_id: str = ""
) -> dict[str, Any]:
    store = _store_for(user_id, tenant_id)
    results: list[dict[str, Any]] = []

    if url:
        try:
            import httpx

            from app.config import settings

            async with httpx.AsyncClient(timeout=settings.http_timeout_default) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                items = resp.json()
                installed_names = {s.name for s in store.list()}
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
            raise HTTPException(status_code=500, detail=f"discover remote failed: {e}") from e
    else:
        for s in store.list():
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

    return {"local": results, "remote": []}
