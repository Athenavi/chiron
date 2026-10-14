"""P7：单一能力代理（app/tools/use_capability.py）单测。

钉住 Reasonix usecap 的核心性质：**代理工具的 schema 字节不随 MCP 增删而变**，
且 MCP（隐藏来源）不进模型可见清单、只能经代理调用。
"""

from __future__ import annotations

import copy

import pytest

from app.tools import use_capability as uc
from app.tools.registry import SOURCE_MCP, registry


@pytest.fixture(autouse=True)
def _clean_mcp() -> None:
    """每个用例前后清掉测试注册的 MCP 工具，避免污染全局注册表。"""
    def _drop() -> None:
        for name in list(registry.list_names()):
            tool = registry.get(name)
            if tool is not None and tool.source == SOURCE_MCP:
                registry.unregister(name)

    _drop()
    yield
    _drop()


def _register_mcp(name: str, desc: str = "an external tool") -> None:
    async def handler(**kwargs: object) -> dict[str, object]:
        return {"ok": True, "echo": kwargs}

    registry.register(
        name=name, description=desc, parameters={"type": "object"}, handler=handler, source=SOURCE_MCP
    )


def test_schema_is_fixed_regardless_of_mcp() -> None:
    before = uc.capability_schema()
    _register_mcp("mcp_alpha")
    after = uc.capability_schema()
    assert before == after, "proxy schema must not change when an MCP tool is added"

    _register_mcp("mcp_beta")
    registry.unregister("mcp_alpha")
    assert uc.capability_schema() == before, "proxy schema must not change when MCP tools churn"


def test_schema_is_isolated_copy() -> None:
    s = uc.capability_schema()
    s["properties"]["capability"]["type"] = "number"
    assert uc.capability_schema()["properties"]["capability"]["type"] == "string"


def test_mcp_tool_not_in_visible_tools() -> None:
    _register_mcp("mcp_hidden")
    names = [t["function"]["name"] for t in registry.to_openai_tools("u1")]
    assert "mcp_hidden" not in names, "hidden-source tools must not enter the visible list"


def test_list_capabilities_includes_hidden() -> None:
    _register_mcp("mcp_hidden")
    caps = [c["name"] for c in registry.list_capabilities("u1")]
    assert "mcp_hidden" in caps


async def test_use_capability_lists_when_no_args() -> None:
    _register_mcp("mcp_hidden")
    out = await uc.use_capability()
    assert any(c["name"] == "mcp_hidden" for c in out["capabilities"])


async def test_use_capability_invokes_hidden_capability() -> None:
    _register_mcp("mcp_echo")
    out = await uc.use_capability("mcp_echo", {"x": 1})
    assert out.get("ok") is True
    assert out.get("echo") == {"x": 1}


async def test_use_capability_rejects_builtin() -> None:
    # 内置工具不经代理（它们本就在清单里）。
    out = await uc.use_capability("read_file", {"path": "a"})
    assert "error" in out
    assert "built-in" in out["error"]


async def test_use_capability_unknown() -> None:
    out = await uc.use_capability("does_not_exist")
    assert "error" in out
    assert "unknown capability" in out["error"]


def test_proxy_tool_is_registered_and_visible() -> None:
    names = [t["function"]["name"] for t in registry.to_openai_tools("u1")]
    assert uc.USE_CAPABILITY_NAME in names


def test_schema_shape() -> None:
    schema = copy.deepcopy(uc.capability_schema())
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"capability", "arguments"}
    assert schema["required"] == ["capability"]
