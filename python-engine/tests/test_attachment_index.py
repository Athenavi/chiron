"""附件结构化索引（``app/knowledge/attachment_index.py``）。

**守的是什么**：索引是跨会话复用的，所以这里的不变量全是"复用"带出来的风险：

1. **确定性 id**：同一 ``asset_id`` 必须永远映射同一行 —— 否则每次发同一个文件都新建
   一份索引，跨会话去重就名存实亡；
2. **不可索引即 None**：provider 不支持该类型 / 用户没配知识库时返回 None，调用方据此
   保持本地分片，不需要区分"失败"与"不支持"；
3. **删除即不可达**：资产被删掉后，索引里的内容不能再被问答命中。
"""

from __future__ import annotations

import pytest

from app.knowledge import attachment_index as mod
from app.knowledge.providers.base import HealthResult


def test_kb_id_is_deterministic_and_per_user() -> None:
    a = mod.attachment_kb_id(7, "u-1")
    assert a == mod.attachment_kb_id(7, "u-1"), "同一用户必须永远是同一个库"
    assert a != mod.attachment_kb_id(7, "u-2"), "不同用户不能共用库（隔离边界）"
    assert a != mod.attachment_kb_id(8, "u-1"), "不同租户不能共用库"


def test_doc_id_is_deterministic_per_asset() -> None:
    a = mod.attachment_doc_id(7, "asset-1")
    assert a == mod.attachment_doc_id(7, "asset-1"), "同一附件必须映射同一行（这就是去重）"
    assert a != mod.attachment_doc_id(7, "asset-2")


def test_attachment_kb_name_is_not_a_user_visible_name() -> None:
    """个人附件库要在列表里被排除，靠的就是这个名字；改了名字而忘了改过滤会漏出来。"""
    assert mod.ATTACHMENT_KB_NAME.startswith("__")


class _Provider:
    def __init__(self, supports: bool) -> None:
        self._supports = supports

    def supports(self, filename: str) -> bool:
        return self._supports


def _patch_registry(monkeypatch: pytest.MonkeyPatch, provider: _Provider | None) -> None:
    async def fake_resolve(user_id: str, **_kw: object):  # type: ignore[no-untyped-def]
        if provider is None:
            return None, None, HealthResult(False, "disabled", "未配置")
        return provider, None, HealthResult(True, "ok")

    monkeypatch.setattr("app.knowledge.providers.registry.resolve_for_user", fake_resolve)


async def test_unsupported_type_is_not_indexed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_registry(monkeypatch, _Provider(supports=False))

    out = await mod.index_attachment(
        tenant_id=7, user_id="u-1", asset_id="a1", name="notes.txt", data=b"hi"
    )

    assert out is None, "不支持的类型必须回落到分片，而不是留下一个永远 pending 的行"


async def test_missing_user_settings_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_registry(monkeypatch, None)

    out = await mod.index_attachment(
        tenant_id=7, user_id="u-1", asset_id="a1", name="doc.pdf", data=b"%PDF"
    )

    assert out is None, "没配知识库是正常路径：附件照常能发，只是不建索引"


async def test_empty_asset_or_bytes_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_registry(monkeypatch, _Provider(supports=True))

    assert await mod.index_attachment(
        tenant_id=7, user_id="u-1", asset_id="", name="doc.pdf", data=b"%PDF"
    ) is None
    assert await mod.index_attachment(
        tenant_id=7, user_id="u-1", asset_id="a1", name="doc.pdf", data=b""
    ) is None


async def test_query_requires_a_question() -> None:
    assert await mod.query_attachment(
        tenant_id=7, user_id="u-1", asset_id="a1", question="  "
    ) is None


async def test_deleted_asset_is_not_answerable(monkeypatch: pytest.MonkeyPatch) -> None:
    """资产删了但索引还在：必须判为不可用，否则"删除"只是从列表里消失。"""

    async def gone(asset_id: str, tenant_id: int, user_id: str) -> bool:
        return False

    monkeypatch.setattr("app.media.attachments._asset_still_exists", gone)

    out = await mod.query_attachment(
        tenant_id=7, user_id="u-1", asset_id="a1", question="合同金额是多少"
    )

    assert out is None


def test_as_dict_accepts_both_json_shapes() -> None:
    assert mod._as_dict({"a": 1}) == {"a": 1}
    assert mod._as_dict('{"a": 1}') == {"a": 1}
    assert mod._as_dict("not json") == {}
    assert mod._as_dict(None) == {}
