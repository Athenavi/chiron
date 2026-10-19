"""自托管 provider —— PageIndex ``local`` 模式。

三条硬约束（都是 SaaS 部署里会真出问题的地方）：

1. **每个知识库一个 ``storage_path``**（``<root>/<tenant>/<kb>``）。PageIndex local 的
   ``browse_documents`` 会列出 storage 里的**全部**文档；所有库共用一个目录，这个工具就会
   跨库、乃至跨租户列出别人的文档。按库分区是隔离的第一道闸。
2. **root 必须在所有副本可见的存储上**（共享卷 / NFS / RWX PVC）。local 模式没有中心服务，
   索引文件就是唯一真相；放在 Pod 本地盘上，多实例下必然"时有时无"。
3. **同一 KB 的写入由队列串行化**（``app/queue/worker.py`` 的 ``kb_index``）。``DocStore``
   的文件锁只在进程内有效，跨实例并发写 ``manifest.json`` 会互相覆盖。

文档类型：local 只支持 **text-based PDF**（PageIndex Flash 的既定边界，见 P0 结论）。其它
类型明确拒绝（``UnsupportedDocument``），而不是"索引出一棵空树"——后者会在检索阶段表现为
"文档明明在、就是查不到"。

LLM：``index`` 与 ``chat`` 都用部署方配置的模型（见 ``app/knowledge/llm.py``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.knowledge.errors import ProviderFailed, UnsupportedDocument
from app.knowledge.llm import resolve_target
from app.knowledge.providers.chat_result import guard_citations, normalize_chat_result
from app.knowledge.providers.base import (
    STATUS_DISABLED,
    STATUS_READY,
    AnswerResult,
    HealthResult,
    ProviderContext,
    SubmitResult,
    PROVIDER_SELFHOSTED,
    doc_prefix,
)

logger = logging.getLogger(__name__)

# local 模式能吃的扩展名（PageIndex Flash 面向 text-based PDF）
_SUPPORTED_SUFFIXES = {".pdf"}


class SelfHostedProvider:
    name = PROVIDER_SELFHOSTED

    def __init__(self, *, root: str, index_model: str, chat_model: str) -> None:
        self._root = root
        self._index_model = index_model
        self._chat_model = chat_model

    # ── 健康检查 ──

    def health(self) -> HealthResult:
        try:
            import pageindex  # noqa: F401
        except Exception as exc:  # noqa: BLE001 — 未安装就是"未启用"，不该让服务起不来
            return HealthResult(False, STATUS_DISABLED, f"pageindex 未安装: {exc}")

        if not self._index_model or not self._chat_model:
            return HealthResult(False, STATUS_DISABLED, "未配置自托管索引/问答模型")

        try:
            root = Path(self._root).expanduser()
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".health"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, STATUS_DISABLED, f"索引目录不可写: {exc}")

        return HealthResult(True, STATUS_READY, "")

    # ── 内部 ──

    def storage_path(self, ctx: ProviderContext) -> str:
        """按 租户/库 分区的索引目录（隔离边界，见模块 docstring）。"""
        return os.path.join(self._root, ctx.tenant_id or "_", ctx.kb_id or "_")

    async def prepare_index(self, ctx: ProviderContext) -> str:
        """返回索引目录（相对 root）。

        由 service 回写 ``knowledge_bases.index_ref``，让"每个库一个位置"在数据里也是显式的
        （配合 0023 的唯一约束 ``(provider, index_ref)``，脏数据会被挡在写入前）。
        """
        rel = os.path.relpath(self.storage_path(ctx), self._root)
        return rel.replace(os.sep, "/")

    def _client(self, ctx: ProviderContext) -> Any:
        from pageindex import PageIndexLocalClient

        index_target = resolve_target(self._index_model)
        chat_target = resolve_target(self._chat_model)
        return PageIndexLocalClient(
            index_model=index_target.model,
            index_backend=index_target.backend() or None,
            chat_model=chat_target.model,
            chat_backend=chat_target.backend() or None,
            storage_path=self.storage_path(ctx),
        )

    @staticmethod
    def _require_supported(filename: str) -> None:
        suffix = Path(filename).suffix.lower()
        if suffix not in _SUPPORTED_SUFFIXES:
            raise UnsupportedDocument(
                f"自托管模式仅支持 PDF（PageIndex local 只处理 text-based PDF），当前为 {suffix or '未知类型'}"
            )

    def supports(self, filename: str) -> bool:
        return Path(filename).suffix.lower() in _SUPPORTED_SUFFIXES

    def _run(self, ctx: ProviderContext, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    # ── 协议实现 ──

    async def submit_document(
        self, ctx: ProviderContext, *, doc_id: str, filename: str, data: bytes, file_type: str
    ) -> SubmitResult:
        self._require_supported(filename or f"{doc_id}.pdf")
        return await asyncio.to_thread(self._submit_sync, ctx, doc_id, filename, data)

    def _submit_sync(
        self, ctx: ProviderContext, doc_id: str, filename: str, data: bytes
    ) -> SubmitResult:
        client = self._client(ctx)
        # 临时文件名 = `<doc_id>-<随机>_<原名>`：
        # ① 前缀是 doc_id → 重建时命中同一份文档（PageIndex 以 basename 为文档名）；
        # ② 保留原名 → 引用里的 doc_name 可读，且能**可验证地**映射回本库文档
        #    （见 chat_result.guard_citations）。
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem)[:60] or "doc"
        fd, path = tempfile.mkstemp(prefix=f"{doc_prefix(doc_id)}-", suffix=f"_{stem}.pdf")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            result = client.submit_document(path, wait=True)
            provider_doc_id = str(result.get("doc_id") or "")
            page_count = 0
            with contextlib.suppress(Exception):
                meta = client.get_document(provider_doc_id)
                page_count = int(meta.get("pageNum") or 0)
            return SubmitResult(doc_id=provider_doc_id, page_count=page_count)
        except Exception as exc:  # noqa: BLE001 — SDK 的失败形态多样，统一收敛
            raise ProviderFailed(f"索引文档失败: {exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    async def delete_document(self, ctx: ProviderContext, *, pageindex_doc_id: str) -> None:
        if not pageindex_doc_id:
            return

        def _delete() -> None:
            client = self._client(ctx)
            with contextlib.suppress(Exception):
                # 文档本就可能不存在（重复删除/重建）；删除失败不该让上层事务回滚
                client.delete_document(pageindex_doc_id)

        await asyncio.to_thread(_delete)

    async def answer(self, ctx: ProviderContext, *, question: str) -> AnswerResult:
        return await asyncio.to_thread(self._answer_sync, ctx, question)

    def _answer_sync(self, ctx: ProviderContext, question: str) -> AnswerResult:
        doc_ids = [d for d in ctx.doc_ids if d]
        if not doc_ids:
            # 与 Cloud 同一条口径：没有已就绪文档就没有可检索的范围。
            # （local 的 storage 本身按库隔离，这一判是为了"状态不一致时宁可报错"，
            #  而不是让模型在库里翻出索引失败的残留内容。）
            raise ProviderFailed("该知识库还没有已索引的文档，请先在知识库页构建索引")
        client = self._client(ctx)
        try:
            raw = client.chat(
                question,
                doc_id=doc_ids,
                citations=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailed(f"检索失败: {exc}") from exc
        return guard_citations(normalize_chat_result(raw), doc_ids, provider=self.name)

    def agent_tools(self, ctx: ProviderContext) -> list[Any]:
        # 注：这里的工具绑定在"该库的 storage"上（client 按库构造），所以
        # browse_documents 只会看到这个库的文档。
        return list(self._client(ctx).agent_tools())

    def agent_instructions(self, ctx: ProviderContext) -> str:
        return str(self._client(ctx).agent_instructions())
