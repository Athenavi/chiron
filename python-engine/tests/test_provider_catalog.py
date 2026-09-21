"""服务提供商目录与「目录驱动路由」的回归测试。

覆盖三件事：
1. 目录解析优先级（DB 覆盖 > env > Settings 字段 > 目录默认）；
2. 具名 provider 的 name 注入（决定 KeyRing keyset 分区 llm:keys:{name}）；
3. GatewayRouter 按目录 model_prefixes 匹配 provider、成本/质量分与探活模型。
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest

from app.config import settings
from app.gateway.provider import ChatResponse, EmbeddingResponse, LLMProvider
from app.gateway.router import GatewayRouter
from app.providers.catalog import (provider_api_key, provider_base_url,
                                   provider_catalog, provider_kind,
                                   provider_requires_key)
from app.providers.named import NamedAnthropicProvider, NamedOpenAIProvider


class StubProvider(LLMProvider):
    """最小 provider 桩：只用于路由选择，不发起真实调用。"""

    def __init__(self, name: str):
        self.name = name

    async def chat_stream(self, messages, model, **kwargs) -> AsyncIterator[ChatResponse]:
        yield ChatResponse(content="", finish_reason="stop")

    async def chat(self, messages, model, **kwargs) -> ChatResponse:
        return ChatResponse(content="", finish_reason="stop")

    async def embed(self, text, model) -> EmbeddingResponse:
        return EmbeddingResponse()

    async def close(self) -> None:
        return None


def _preset(provider_id: str, **overrides) -> dict:
    preset = {"id": provider_id, "kind": "openai", "base_url": ""}
    preset.update(overrides)
    return preset


# ── 目录 ──


def test_catalog_covers_builtin_providers():
    ids = {p["id"] for p in provider_catalog()}
    for expected in ("openai", "anthropic", "deepseek", "moonshot", "zhipu",
                     "dashscope", "openrouter", "siliconflow", "opencode", "opencode-go",
                     "ollama", "custom"):
        assert expected in ids, f"目录缺少 provider {expected}"


def test_catalog_remote_entries_win_but_fallback_fills_gaps(monkeypatch):
    remote = [{"id": "deepseek", "kind": "openai", "base_url": "https://remote.example/v1",
               "label": "远端覆盖"}]
    monkeypatch.setattr(settings, "llm_provider_catalog", remote, raising=False)

    catalog = {p["id"]: p for p in provider_catalog()}
    # 网关下发的同名项覆盖兜底项
    assert catalog["deepseek"]["base_url"] == "https://remote.example/v1"
    assert catalog["deepseek"]["label"] == "远端覆盖"
    # 网关未下发的项由兜底目录补齐
    assert catalog["moonshot"]["base_url"] == "https://api.moonshot.cn/v1"


def test_provider_kind_defaults_to_openai_for_unknown():
    assert provider_kind(_preset("x")) == "openai"
    assert provider_kind(_preset("x", kind="anthropic")) == "anthropic"
    assert provider_kind(_preset("x", kind="weird")) == "openai"


# ── 端点 / key 解析优先级 ──


def test_base_url_prefers_db_override(monkeypatch):
    monkeypatch.setattr(settings, "provider_overrides",
                        {"moonshot_base_url": "https://gw.internal/v1/"}, raising=False)
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://env.example/v1")
    assert provider_base_url(_preset("moonshot", base_url="https://api.moonshot.cn/v1")) \
        == "https://gw.internal/v1"


def test_base_url_env_beats_settings_field(monkeypatch):
    monkeypatch.setattr(settings, "provider_overrides", {}, raising=False)
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://env.example/v1/")
    assert provider_base_url(_preset("moonshot", base_url="https://api.moonshot.cn/v1")) \
        == "https://env.example/v1"


def test_base_url_uses_settings_field_for_legacy_providers(monkeypatch):
    """deepseek_base_url 由 Settings 字段（DEEPSEEK_BASE_URL）提供，优先级高于目录默认。"""
    monkeypatch.setattr(settings, "provider_overrides", {}, raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    assert provider_base_url(_preset("deepseek", base_url="https://fallback.example")) \
        == settings.deepseek_base_url


def test_base_url_falls_back_to_catalog_default(monkeypatch):
    monkeypatch.setattr(settings, "provider_overrides", {}, raising=False)
    monkeypatch.delenv("ZHIPU_BASE_URL", raising=False)
    assert provider_base_url(_preset("zhipu", base_url="https://open.bigmodel.cn/api/paas/v4")) \
        == "https://open.bigmodel.cn/api/paas/v4"


def test_api_key_resolution_order(monkeypatch):
    # DB 覆盖优先
    monkeypatch.setattr(settings, "provider_overrides", {"moonshot_api_key": "db-key"}, raising=False)
    monkeypatch.setenv("MOONSHOT_API_KEY", "env-key")
    assert provider_api_key(_preset("moonshot")) == "db-key"

    # 其次目录声明的 env 名
    monkeypatch.setattr(settings, "provider_overrides", {}, raising=False)
    assert provider_api_key(_preset("moonshot", api_key_env="MOONSHOT_API_KEY")) == "env-key"

    # 未配置时为空（引擎据此跳过注册）
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    assert provider_api_key(_preset("moonshot", api_key_env="MOONSHOT_API_KEY")) == ""


def test_requires_key_flag():
    assert provider_requires_key(_preset("openai")) is True
    assert provider_requires_key(_preset("ollama", requires_key=False)) is False


# ── 具名 provider ──


def test_named_provider_injects_instance_name():
    """provider 名决定 keyset 分区，不能沿用基类默认的 openai/anthropic。"""
    assert NamedOpenAIProvider("moonshot", api_key="k").name == "moonshot"
    assert NamedOpenAIProvider("deepseek", api_key="k").name == "deepseek"
    assert NamedAnthropicProvider("custom-anthropic", api_key="k").name == "custom-anthropic"


# ── 路由 ──


def _router(providers: dict, catalog: list[dict] | None = None) -> GatewayRouter:
    return GatewayRouter(providers=providers, provider_catalog=catalog)


def test_find_candidates_uses_catalog_prefixes():
    providers = {
        "moonshot": StubProvider("moonshot"),
        "openai": StubProvider("openai"),
        "zhipu": StubProvider("zhipu"),
    }
    catalog = [
        {"id": "moonshot", "model_prefixes": ["kimi", "moonshot"]},
        {"id": "openai", "model_prefixes": ["gpt"]},
        {"id": "zhipu", "model_prefixes": ["glm"]},
    ]
    router = _router(providers, catalog)

    assert [p.name for p in router._find_candidates("kimi-k2")] == ["moonshot"]
    assert [p.name for p in router._find_candidates("glm-4-plus")] == ["zhipu"]
    # 目录未命中 → 返回空候选由 _select 兜底（此处只验证前缀匹配未误伤）
    assert [p.name for p in router._find_candidates("gpt-4o")] == ["openai"]


def test_find_candidates_without_catalog_keeps_legacy_rules():
    providers = {
        "anthropic": StubProvider("anthropic"),
        "deepseek": StubProvider("deepseek"),
        "openai": StubProvider("openai"),
    }
    router = _router(providers)
    assert [p.name for p in router._find_candidates("claude-3-5-sonnet")] == ["anthropic"]
    assert [p.name for p in router._find_candidates("deepseek-chat")] == ["deepseek"]
    assert [p.name for p in router._find_candidates("gpt-4o")] == ["openai"]
    # 未知模型名兜底到 openai（历史行为）
    assert [p.name for p in router._find_candidates("mystery-model")] == ["openai"]


def test_custom_provider_matches_by_id_prefix():
    """目录外的自定义 provider（合成目录项 model_prefixes=[id/]）也能被路由命中。"""
    providers = {"my-gateway": StubProvider("my-gateway"), "openai": StubProvider("openai")}
    router = _router(providers, [{"id": "my-gateway", "model_prefixes": ["my-gateway/"]}])
    assert [p.name for p in router._find_candidates("my-gateway/gpt-4o")] == ["my-gateway"]


def test_weighted_select_handles_zero_cost_provider():
    """自托管 provider 目录成本为 0，不能触发除零。"""
    catalog = [{"id": "ollama", "cost": 0.0, "quality": 0.6},
               {"id": "openai", "cost": 5.0, "quality": 0.9}]
    router = _router({"ollama": StubProvider("ollama"), "openai": StubProvider("openai")}, catalog)
    picked = router._weighted_select([router._providers["ollama"], router._providers["openai"]])
    assert picked.name in {"ollama", "openai"}


def test_provider_cost_and_quality_from_catalog():
    catalog = [{"id": "moonshot", "cost": 1.2, "quality": 0.85}]
    router = _router({"moonshot": StubProvider("moonshot")}, catalog)
    assert router._provider_cost("moonshot") == pytest.approx(1.2)
    assert router._provider_quality("moonshot") == pytest.approx(0.85)
    # 目录未收录 → 内建兜底（openai 5.0 / 未知 5.0）
    assert router._provider_cost("unknown-provider") == 5.0


def test_probe_model_prefers_catalog_then_builtin_table():
    catalog = [{"id": "moonshot", "probe_model": "moonshot-v1-8k"},
               {"id": "custom", "probe_model": ""}]
    router = _router({"moonshot": StubProvider("moonshot"), "custom": StubProvider("custom")}, catalog)
    assert router._probe_model("moonshot") == "moonshot-v1-8k"
    # 目录未给探活模型时回落到内建表
    assert router._probe_model("deepseek") == "deepseek-chat"
    # 两处都没有 → 不探活（返回空）
    assert router._probe_model("custom") == ""


@pytest.mark.asyncio
async def test_health_check_skips_probe_without_catalog_model():
    """无探活模型的 provider 直接报 ok（skipped），避免 404 误开熔断。"""
    router = _router({"my-gateway": StubProvider("my-gateway")})
    health = await router.health_check()
    assert health["my-gateway"]["status"] == "ok"
    assert health["my-gateway"]["probe"] == "skipped"
