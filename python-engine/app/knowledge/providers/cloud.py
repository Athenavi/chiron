"""Cloud provider —— PageIndex Cloud（用户自带 API key）。

与自托管的四点差异，直接决定实现形状：

* **负责索引与存储的是平台**（PageIndex Cloud 做 OCR、图片理解、树构建与托管存储），
  所以这里不碰 ``storage_path``，也不受"只吃 text-based PDF"的限制。
* **凭据是用户级的**（``user_knowledge_settings.cloud_api_key_enc``，密文），实例按用户构造 ——
  一个 provider 实例只服务一个用户，绝不缓存跨用户的 client。
* **folder 是两层**：顶层以 **user_id** 命名（这个用户在 PageIndex 账号里的"Chiron 空间"），
  其下每个知识库一个子 folder。作用是**组织与审计**（哪些文档是 Chiron 放进去的），
  **不是安全边界** —— 见下面"范围"一节。
* **范围靠引擎侧兜**：pageindex 的 ``doc_id``/``folder_id`` 官方语义是 "steer discovery"，
  不是硬限制。所以真正的加固有三条：① 库里没有已就绪文档时**拒绝检索**，绝不放宽到
  整个账号；② 检索结果里的引用必须能映射回本库文档，越界即**拒绝返回**（``guard_citations``）；
  ③ 账号级的浏览工具（``agent_tools``）**不接线**给 agent。

``health()`` 只做**本地可判定**的检查（SDK 是否可用、key 是否配置）；真正的连通性验证是
``verify()``（一次 ``list_documents``），由"测试连接"这类显式动作触发 —— 启动时对每个用户
发一次网络请求，会把"某用户 key 失效"变成整个服务的启动延迟。
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
from app.knowledge.providers.base import (
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_READY,
    AnswerResult,
    HealthResult,
    ProviderContext,
    SubmitResult,
    PROVIDER_CLOUD,
    doc_prefix,
)
from app.knowledge.providers.chat_result import guard_citations, normalize_chat_result

logger = logging.getLogger(__name__)

# Cloud 接受的类型（官方文档：PDF / PPTX / Word）
_SUPPORTED_SUFFIXES = {".pdf", ".pptx", ".ppt", ".docx", ".doc"}

#: folder 名先本地清洗一次：PageIndex 侧也会校验，把非法名发出去只会换来一个远端报错。
_UNSAFE_FOLDER_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _folder_name(value: str, fallback: str) -> str:
    cleaned = _UNSAFE_FOLDER_RE.sub("-", value or "").strip("-")
    return cleaned[:60] or fallback


class CloudProvider:
    name = PROVIDER_CLOUD

    def __init__(
        self,
        *,
        api_key: str,
        chat_model: str,
        folder_id: str = "",
        user_folder_id: str = "",
    ) -> None:
        self._api_key = api_key
        self._chat_model = chat_model
        #: 库级 folder（= knowledge_bases.index_ref）
        self._folder_id = folder_id
        #: 用户级 folder（= user_knowledge_settings.cloud_folder_id）
        self._user_folder_id = user_folder_id

    # ── 健康检查 ──

    def health(self) -> HealthResult:
        try:
            import pageindex  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, STATUS_DISABLED, f"pageindex 未安装: {exc}")
        if not self._api_key:
            return HealthResult(False, STATUS_DISABLED, "未配置 PageIndex Cloud API key")
        if not self._chat_model:
            return HealthResult(False, STATUS_DISABLED, "未配置问答模型")
        return HealthResult(True, STATUS_READY, "")

    def verify(self) -> HealthResult:
        """真实连通性验证（显式触发，不在启动路径上）。"""
        if not self._api_key:
            return HealthResult(False, STATUS_DISABLED, "未配置 PageIndex Cloud API key")
        try:
            self._client().list_documents(limit=1)
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, STATUS_ERROR, f"PageIndex Cloud 不可用: {exc}")
        return HealthResult(True, STATUS_READY, "")

    # ── folder：用户空间 → 库 ──

    @staticmethod
    def _user_folder_name(ctx: ProviderContext) -> str:
        """顶层 folder **以 user_id 命名**：把 Chiron 放进去的文档与用户本人在
        PageIndex 控制台上传的文档区分开。"""
        return _folder_name(ctx.user_id, "chiron-user")

    @staticmethod
    def _kb_folder_name(ctx: ProviderContext) -> str:
        return _folder_name(ctx.kb_id, "kb")

    def _find_folder(self, client: Any, name: str, parent_id: str | None) -> str:
        try:
            data = client.list_folders(parent_folder_id=parent_id)
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailed(f"列取 PageIndex folder 失败: {exc}") from exc
        for item in (data or {}).get("folders") or []:
            if not isinstance(item, dict) or str(item.get("name") or "") != name:
                continue
            if (item.get("parent_folder_id") or None) == parent_id:
                return str(item.get("id") or "")
        return ""

    def _ensure_folder(self, client: Any, name: str, parent_id: str | None) -> str:
        existing = self._find_folder(client, name, parent_id)
        if existing:
            return existing
        try:
            created = client.create_folder(name, parent_folder_id=parent_id)
        except Exception as exc:  # noqa: BLE001 — 并发下可能已被另一次任务建好
            again = self._find_folder(client, name, parent_id)
            if again:
                return again
            raise ProviderFailed(f"创建 PageIndex folder 失败: {exc}") from exc
        folder_id = str(((created or {}).get("folder") or {}).get("id") or "")
        if not folder_id:
            raise ProviderFailed("创建 PageIndex folder 未返回 id")
        return folder_id

    def _prepare_index_sync(self, ctx: ProviderContext) -> str:
        if self._folder_id:
            return self._folder_id
        client = self._client()
        self._user_folder_id = self._user_folder_id or self._ensure_folder(
            client, self._user_folder_name(ctx), None
        )
        self._folder_id = self._ensure_folder(
            client, self._kb_folder_name(ctx), self._user_folder_id
        )
        return self._folder_id

    async def prepare_index(self, ctx: ProviderContext) -> str:
        """确保 folder 层级存在，返回**库 folder id**（service 据此回写 ``index_ref``）。"""
        return await asyncio.to_thread(self._prepare_index_sync, ctx)

    @property
    def user_folder_id(self) -> str:
        """已建/已知的用户级 folder id（空 = 还没建过）。"""
        return self._user_folder_id

    # ── 内部 ──

    def _client(self) -> Any:
        from pageindex import PageIndexClient

        chat_target = resolve_target(self._chat_model)
        return PageIndexClient(
            api_key=self._api_key,
            index="cloud",
            chat_model=chat_target.model,
            chat_backend=chat_target.backend() or None,
        )

    @staticmethod
    def _require_supported(filename: str) -> None:
        suffix = Path(filename).suffix.lower()
        if suffix not in _SUPPORTED_SUFFIXES:
            raise UnsupportedDocument(
                f"Cloud 模式支持 PDF/PPTX/Word，当前为 {suffix or '未知类型'}"
            )

    def supports(self, filename: str) -> bool:
        return Path(filename).suffix.lower() in _SUPPORTED_SUFFIXES

    @staticmethod
    def _temp_file(doc_id: str, filename: str) -> tuple[int, str]:
        """临时文件名 = ``<doc_id>-<随机>_<原名>``。

        两件事同时成立：① 引用里的 doc_name **可读**（用户看到自己的文件名，
        而不是一串随机串）；② 前缀是 doc_id，引用能**可验证地**映射回本库文档
        （见 ``chat_result.guard_citations``）。
        """
        suffix = Path(filename).suffix.lower() or ".pdf"
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem)[:60] or "doc"
        return tempfile.mkstemp(prefix=f"{doc_prefix(doc_id)}-", suffix=f"_{stem}{suffix}")

    # ── 协议实现 ──

    async def submit_document(
        self, ctx: ProviderContext, *, doc_id: str, filename: str, data: bytes, file_type: str
    ) -> SubmitResult:
        self._require_supported(filename or f"{doc_id}.pdf")
        return await asyncio.to_thread(self._submit_sync, ctx, doc_id, filename, data)

    def _submit_sync(
        self, ctx: ProviderContext, doc_id: str, filename: str, data: bytes
    ) -> SubmitResult:
        client = self._client()
        folder_id = self._folder_id or self._prepare_index_sync(ctx)
        fd, path = self._temp_file(doc_id, filename)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            # wait=True：Cloud 侧是异步处理，但这里已经是队列任务，阻塞在 worker 上
            # 比"提交完就返回、状态无人跟踪"更可预期
            result = client.submit_document(path, folder_id=folder_id or None, wait=True)
            provider_doc_id = str(result.get("doc_id") or "")
            page_count = 0
            with contextlib.suppress(Exception):
                meta = client.get_document(provider_doc_id)
                page_count = int(meta.get("pageNum") or 0)
            return SubmitResult(doc_id=provider_doc_id, page_count=page_count)
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailed(f"提交到 PageIndex Cloud 失败: {exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    async def delete_document(self, ctx: ProviderContext, *, pageindex_doc_id: str) -> None:
        if not pageindex_doc_id:
            return

        def _delete() -> None:
            with contextlib.suppress(Exception):
                self._client().delete_document(pageindex_doc_id)

        await asyncio.to_thread(_delete)

    async def answer(self, ctx: ProviderContext, *, question: str) -> AnswerResult:
        return await asyncio.to_thread(self._answer_sync, ctx, question)

    def _answer_sync(self, ctx: ProviderContext, question: str) -> AnswerResult:
        doc_ids = [d for d in ctx.doc_ids if d]
        if not doc_ids:
            # **绝不放宽到"整个账号"**：Cloud 的账号是这个用户的空间，里面可能有他的
            # 其它知识库、乃至他本人在 PageIndex 控制台上传的文档。库里没有已就绪文档时，
            # 正确答案是"没有内容可检索"，而不是"去账号里别处找"。
            raise ProviderFailed("该知识库还没有已索引的文档，请先在知识库页构建索引")
        client = self._client()
        try:
            raw = client.chat(question, doc_id=doc_ids, citations=True)
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailed(f"PageIndex Cloud 检索失败: {exc}") from exc
        return guard_citations(normalize_chat_result(raw), doc_ids, provider=self.name)

    def agent_tools(self, ctx: ProviderContext) -> list[Any]:
        """**不要把这个接到 agent 的工具面上。**

        PageIndex 的 ``browse_documents`` / ``get_*`` 作用于**整个账号**（官方文档：
        context "does not restrict tool access"）。交给 agent 等于给它一个能读该用户
        PageIndex 账号里全部文档的入口 —— 超出"这个知识库"的授权范围。

        保留实现只为满足协议（当前没有任何调用方：``grep agent_tools`` 在业务代码里为空）。
        """
        return list(self._client().agent_tools())

    def agent_instructions(self, ctx: ProviderContext) -> str:
        return str(self._client().agent_instructions())
