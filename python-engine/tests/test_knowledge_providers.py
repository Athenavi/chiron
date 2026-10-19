"""引擎侧 provider 验收（S4 之后引擎只有 ``RemoteProvider`` 一种实现）。

进程内实现（selfhosted/cloud）已删除 —— 它要求引擎装 PageIndex SDK，而
``pageindex → litellm → openai<3`` 与引擎固定的 ``openai 3.x`` 无法在同一环境共存。
这里验证的是"引擎怎么用远程服务"：地址/令牌校验、越界守卫（纵深防御）、
索引不在引擎侧发生。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.knowledge.errors import KnowledgeError, ProviderFailed, ProviderUnavailable
from app.knowledge.providers import registry
from app.knowledge.providers.base import ProviderContext
from app.knowledge.service import serialize_answer
from app.knowledge.providers.base import AnswerResult, Citation


def _ctx(**overrides) -> ProviderContext:
    base = {"kb_id": "kb-1", "tenant_id": "t-1", "user_id": "u-1"}
    base.update(overrides)
    return ProviderContext(**base)


@pytest.fixture(autouse=True)
def _service_env(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_service_url", "", raising=False)
    monkeypatch.setattr(settings, "internal_token", "", raising=False)
    yield


# ── 健康：只做本地判定（不在启动路径上触网）──


def test_health_disabled_without_service_url() -> None:
    result = registry.selfhosted_health()
    assert result.ok is False
    assert "KNOWLEDGE_SERVICE_URL" in result.detail


def test_health_disabled_without_internal_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "knowledge_service_url", "http://pi:8100", raising=False)
    assert registry.selfhosted_health().ok is False


def test_health_ready_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "knowledge_service_url", "http://pi:8100", raising=False)
    monkeypatch.setattr(settings, "internal_token", "tok", raising=False)
    assert registry.selfhosted_health().ok is True


def test_cloud_requires_user_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "knowledge_service_url", "http://pi:8100", raising=False)
    monkeypatch.setattr(settings, "internal_token", "tok", raising=False)
    result = registry.build_provider("cloud").health()
    assert result.ok is False
    assert "API key" in result.detail


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(KnowledgeError):
        registry.build_provider("milvus")


# ── 能力判定：前缀口径与服务侧一致 ──


def test_supports_by_mode() -> None:
    assert registry.build_provider("selfhosted").supports("a.pdf") is True
    assert registry.build_provider("selfhosted").supports("a.docx") is False
    assert registry.build_provider("cloud").supports("a.docx") is True
    assert registry.build_provider("cloud").supports("a.txt") is False


# ── 检索：越界守卫（纵深防御）──


@pytest.mark.asyncio
async def test_answer_passes_through_citations(monkeypatch) -> None:
    provider = registry.build_provider("selfhosted")
    monkeypatch.setattr(
        provider,
        "_post",
        AsyncMock(
            return_value={
                "answer": "答案",
                "citations": [{"doc_name": "3f2a1b4c5d6e7f809a0b1c2d3e4f5a6b-x_报告.pdf", "page": 2}],
            }
        ),
    )
    result = await provider.answer(_ctx(doc_ids=("3f2a1b4c-5d6e-7f80-9a0b-1c2d3e4f5a6b",)), question="q")
    assert result.answer == "答案"
    assert result.citations[0].page == 2


@pytest.mark.asyncio
async def test_answer_rejects_out_of_scope_citations(monkeypatch) -> None:
    provider = registry.build_provider("selfhosted")
    monkeypatch.setattr(
        provider,
        "_post",
        AsyncMock(return_value={"answer": "a", "citations": [{"doc_name": "deadbeef-x.pdf"}]}),
    )
    with pytest.raises(ProviderFailed):
        await provider.answer(_ctx(doc_ids=("doc-1",)), question="q")


@pytest.mark.asyncio
async def test_indexing_is_not_done_by_the_engine() -> None:
    """引擎不索引：调用 submit_document 必须是**显式失败**，而不是悄悄走进程内实现。"""
    provider = registry.build_provider("selfhosted")
    with pytest.raises(ProviderUnavailable):
        await provider.submit_document(
            _ctx(), doc_id="doc-1", filename="a.pdf", data=b"%PDF", file_type="pdf"
        )


def test_serialize_answer_shape() -> None:
    out = serialize_answer(
        AnswerResult(answer="答案", citations=[Citation(doc_name="a.pdf", page=2, snippet="s")])
    )
    assert out["answer"] == "答案"
    assert out["citations"][0]["page"] == 2


def test_agent_tools_are_not_exposed() -> None:
    """账号级浏览工具不接线给 agent（它作用于用户整个 PageIndex 账号）。"""
    provider = registry.build_provider("selfhosted")
    assert provider.agent_tools(_ctx()) == []
    assert provider.agent_instructions(_ctx()) == ""
