"""知识库领域包（PageIndex 版）。

旧的 ``app/rag/``（切块 + 向量 + 全文检索）在本轮重构中整体退场；这里只保留与
provider 无关的基础设施：

* ``crypto`` —— 用户级 Cloud key 的 AES-256-GCM 加解密（与 Go 侧同构）；
* ``blob``   —— 原文对象存储（local 共享卷 / S3）；
* ``providers`` / ``service`` / ``api`` 在后续阶段落入本包。

依赖方向：``knowledge`` 不反向依赖 ``api``/``tools``；provider 实现只依赖本包的
接口与 PageIndex SDK，便于后续接入更多检索引擎。
"""

from __future__ import annotations

from app.knowledge import blob, crypto

__all__ = ["blob", "crypto"]
