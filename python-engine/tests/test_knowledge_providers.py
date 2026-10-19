"""P2 验收：provider 注册表、LLM 解析与答案序列化。

不触网、不连库：只验证"配置 → 状态判定"的纯逻辑。真实的索引/检索需要 LLM 凭据，
由 P0 的结论决定放到有凭据的环境验证。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.knowledge.errors import KnowledgeError
from app.knowledge.llm import resolve_target
from app.knowledge.providers import registry
from app.knowledge.providers.base import (
    STATUS_DISABLED,
    STATUS_READY,
    AnswerResult,
    Citation,
)
from app.knowledge.service import serialize_answer


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "knowledge_selfhosted_root", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "knowledge_selfhosted_index_model", "", raising=False)
    monkeypatch.setattr(settings, "knowledge_selfhosted_chat_model", "", raising=False)
    monkeypatch.setattr(settings, "knowledge_cloud_chat_model", "", raising=False)
    yield


def test_selfhosted_disabled_without_models() -> None:
    result = registry.selfhosted_health()
    assert result.ok is False
    assert result.status == STATUS_DISABLED
    assert "模型" in result.detail


def test_selfhosted_ready_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "knowledge_selfhosted_index_model", "gpt-4o-mini", raising=False)
    monkeypatch.setattr(settings, "knowledge_selfhosted_chat_model", "gpt-4o-mini", raising=False)
    result = registry.selfhosted_health()
    assert result.ok is True
    assert result.status == STATUS_READY


def test_selfhosted_root_unwritable_is_not_ready(monkeypatch, tmp_path) -> None:
    # 用一个"父路径是文件"的位置让 mkdir 必然失败：目录不可写必须是 disabled 而不是异常
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(settings, "knowledge_selfhosted_root", str(blocker / "sub"), raising=False)
    monkeypatch.setattr(settings, "knowledge_selfhosted_index_model", "gpt-4o-mini", raising=False)
    monkeypatch.setattr(settings, "knowledge_selfhosted_chat_model", "gpt-4o-mini", raising=False)
    result = registry.selfhosted_health()
    assert result.ok is False
    assert result.status == STATUS_DISABLED


def test_cloud_requires_key() -> None:
    result = registry.build_provider("cloud", cloud_api_key="").health()
    assert result.ok is False
    assert "API key" in result.detail


def test_cloud_ready_with_key_and_model(monkeypatch) -> None:
    monkeypatch.setattr(settings, "knowledge_cloud_chat_model", "gpt-4o-mini", raising=False)
    result = registry.build_provider("cloud", cloud_api_key="fake-key").health()
    assert result.ok is True


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(KnowledgeError):
        registry.build_provider("nope")


def test_resolve_target_normalizes_model() -> None:
    target = resolve_target("gpt-4o-mini")
    assert target.model.startswith("openai/")
    assert target.model.endswith("gpt-4o-mini")


def test_resolve_target_keeps_unknown_model_as_is() -> None:
    target = resolve_target("some-local-model")
    assert target.model == "some-local-model"


def test_serialize_answer_shape() -> None:
    out = serialize_answer(
        AnswerResult(answer="答案", citations=[Citation(doc_name="a.pdf", page=2, snippet="s")])
    )
    assert out["answer"] == "答案"
    assert out["citations"][0]["page"] == 2
    assert out["citations"][0]["doc_name"] == "a.pdf"


def test_answer_never_widens_scope_to_the_whole_account(tmp_path, monkeypatch) -> None:
    """没有已索引文档时**不得**退化成"整个账号"检索。

    Cloud 的账号是这个用户的空间（可能含他的其它知识库或他本人上传的文档），
    local 的 storage 虽然按库隔离，但状态不一致时也不该让模型在库里乱翻 ——
    两个 provider 都必须以"没有可检索范围"作答。
    """
    from app.knowledge.errors import ProviderFailed
    from app.knowledge.providers.base import ProviderContext
    from app.knowledge.providers.cloud import CloudProvider
    from app.knowledge.providers.selfhosted import SelfHostedProvider

    ctx = ProviderContext(kb_id="kb-1", tenant_id="t-1", user_id="u-1", doc_ids=())

    cloud = CloudProvider(api_key="fake", chat_model="gpt-4o-mini")
    with pytest.raises(ProviderFailed):
        cloud._answer_sync(ctx, "q")  # noqa: SLF001

    monkeypatch.setattr(settings, "knowledge_selfhosted_root", str(tmp_path), raising=False)
    local = SelfHostedProvider(root=str(tmp_path), index_model="m", chat_model="m")
    with pytest.raises(ProviderFailed):
        local._answer_sync(ctx, "q")  # noqa: SLF001


# ── 引用归属守卫（跨库/跨范围内容的第二道防线）──


def test_citation_scope_uses_dashless_prefix() -> None:
    """uuid 自带 4 个连字符：前缀必须去连字符，否则**合法引用会被误判成越界**。"""
    from app.knowledge.providers.base import citation_in_scope, doc_prefix

    doc_id = "3f2a1b4c-5d6e-7f80-9a0b-1c2d3e4f5a6b"
    own = f"{doc_prefix(doc_id)}-ab12cd_报告.pdf"
    assert citation_in_scope(own, [doc_id]) is True
    assert citation_in_scope("deadbeef-ab12cd_别人的.pdf", [doc_id]) is False
    # 解析不出前缀（PageIndex 改写了文档名）→ 不判越界，避免误杀
    assert citation_in_scope("报告.pdf", [doc_id]) is True


def test_guard_citations_rejects_out_of_scope() -> None:
    from app.knowledge.errors import ProviderFailed
    from app.knowledge.providers.base import doc_prefix
    from app.knowledge.providers.chat_result import guard_citations

    doc_id = "3f2a1b4c-5d6e-7f80-9a0b-1c2d3e4f5a6b"
    in_scope = AnswerResult(
        answer="a", citations=[Citation(doc_name=f"{doc_prefix(doc_id)}-x_报告.pdf")]
    )
    assert guard_citations(in_scope, [doc_id], provider="cloud").answer == "a"

    stray = AnswerResult(answer="a", citations=[Citation(doc_name="deadbeef-x_别人的.pdf")])
    with pytest.raises(ProviderFailed):
        guard_citations(stray, [doc_id], provider="cloud")


@pytest.mark.asyncio
async def test_cloud_folder_hierarchy_is_user_then_kb(monkeypatch) -> None:
    """folder 层级：顶层以 **user_id** 命名，其下是库 folder；重复调用幂等。"""
    from app.knowledge.providers.base import ProviderContext
    from app.knowledge.providers.cloud import CloudProvider

    created: list[tuple[str, str | None]] = []

    fake = MagicMock()
    fake.list_folders = MagicMock(return_value={"folders": []})
    fake.create_folder = MagicMock(
        side_effect=lambda name, parent_folder_id=None: (
            created.append((name, parent_folder_id)) or {"folder": {"id": f"f-{name}"}}
        )
    )

    provider = CloudProvider(api_key="k", chat_model="m")
    monkeypatch.setattr(provider, "_client", lambda: fake)
    ctx = ProviderContext(kb_id="kb-9", tenant_id="t-1", user_id="u-7")

    assert await provider.prepare_index(ctx) == "f-kb-9"
    assert created == [("u-7", None), ("kb-9", "f-u-7")]
    assert provider.user_folder_id == "f-u-7"

    created.clear()
    assert await provider.prepare_index(ctx) == "f-kb-9"
    assert created == []  # 已建好，不再创建


def test_selfhosted_prepare_index_is_tenant_scoped() -> None:
    from app.knowledge.providers.base import ProviderContext
    from app.knowledge.providers.selfhosted import SelfHostedProvider

    provider = SelfHostedProvider(root="/srv/kb", index_model="m", chat_model="m")
    ctx = ProviderContext(kb_id="kb-1", tenant_id="t-1", user_id="u-1")
    import asyncio

    assert asyncio.run(provider.prepare_index(ctx)) == "t-1/kb-1"
