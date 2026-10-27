"""本地运行时：对话 / Agent / 模型的最小闭环。

模块划分（都只依赖 ``app.config`` / ``app.storage``，不认识 FastAPI 请求对象）：

=================  ==========================================================================
``schema``         建表 DDL（``CREATE TABLE IF NOT EXISTS``，幂等）
``store``          PostgreSQL 数据访问层（会话 / 消息 / Agent）与 ``store_for(state)``
``inference``      loopback 推理代理客户端、``CHIRON_LOCAL_MODELS`` 解析、``inference_for(state)``
``chat``           对话编排（历史 → messages → 推理 → 落库 → SSE 切块）
=================  ==========================================================================

本 ``__init__`` **刻意不 import 上面的子模块**：``app.api.chat`` 会从 ``app.runtime.chat``
导入，而 ``app.runtime.chat`` 又会导入 ``app.runtime.inference``。若在这里做 re-export，
三个模块之间会绕回包初始化，容易出现半初始化模块。需要什么就显式 ``from app.runtime.x import y``。
"""

from __future__ import annotations

__all__ = ["chat", "inference", "schema", "store"]
