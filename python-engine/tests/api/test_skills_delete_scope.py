"""DELETE /v1/skills/{name} 的 scope 行为 —— 回归保护。

历史缺陷（本次补齐）：
1. 删除端点**不接受 scope**，只能删 user 私有目录；租户共享层（团队资产）的删除
   在代码里被标注为"本期未实现"；
2. 更隐蔽的是：``store.delete`` 只作用于**写目标目录** —— 若该技能其实来自别的层，
   删除是静默空操作，端点却回 200，调用方会以为删掉了团队资产。

本文件锁死三条：
* 缺省 / ``private`` 删私有目录（既有行为不变）；
* ``scope=tenant`` 删租户共享层；
* **目标层里没有该技能 → 404**（而不是假成功）。
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.main import create_app
from app.skill.store import SkillDef, SkillStore

TENANT = "t1"
USER = "u1"


@pytest.fixture(autouse=True)
def _isolated_skill_root(tmp_path, monkeypatch):
    """把技能根目录指到临时目录，避免污染真实 data/skills。"""
    monkeypatch.setenv("SKILL_STORE_PATH", str(tmp_path))
    return tmp_path


def _seed(scope: str, name: str) -> None:
    SkillStore(tenant_id=TENANT, user_id=USER, scope=scope).save(
        SkillDef(name=name, description=f"{scope} skill", exec_type="prompt")
    )


async def _delete(name: str, scope: str = ""):
    app = create_app()
    params = {"user_id": USER, "tenant_id": TENANT}
    if scope:
        params["scope"] = scope
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        return await client.delete(f"/v1/skills/{name}", params=params)


async def test_delete_private_by_default():
    """缺省删除：落在调用者私有目录（既有行为不变）。"""
    _seed("private", "s-private")

    resp = await _delete("s-private")

    assert resp.status_code == 200
    assert resp.json()["scope"] == "user"


async def test_delete_tenant_shared_scope():
    """scope=tenant：删租户共享层（团队资产）。"""
    _seed("tenant", "s-tenant")

    resp = await _delete("s-tenant", scope="tenant")

    assert resp.status_code == 200
    assert resp.json()["scope"] == "tenant"


async def test_wrong_scope_is_404_and_private_copy_survives():
    """技能只在私有层，却请求删租户层 → 必须 404，且私有层那份**不受影响**。

    这正是 ``store.delete`` 只作用于写目标所带来的陷阱：旧实现会静默空操作并回 200。
    """
    _seed("private", "s-only-private")

    resp = await _delete("s-only-private", scope="tenant")

    assert resp.status_code == 404
    assert "not in scope" in resp.json()["detail"]
    # 关键：私有层的那份必须还在（没有被误删）
    still_there = SkillStore(tenant_id=TENANT, user_id=USER, scope="private").get(
        "s-only-private"
    )
    assert still_there is not None


async def test_invalid_scope_is_400():
    _seed("private", "s-x")

    resp = await _delete("s-x", scope="bogus")

    assert resp.status_code == 400


async def test_delete_missing_skill_is_404():
    resp = await _delete("no-such-skill")

    assert resp.status_code == 404
