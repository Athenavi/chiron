"""知识库租户口径的**唯一**定义处（API 层、Agent 工具层、队列 worker 共用）。

这里集中两类容易写歪的判定：

1. 可见性（谁能读）：历史上被抄成了四份（列表 SQL、详情 SQL、文档/检索/预览三处 Python
   gate、工具层 kb_search 的内联判断），口径互不相同 —— 元数据认租户、正文与检索不认，
   于是"标题看不到、整篇文档却能整篇读走"；preview 那处还把条件写反，public 库的非 owner
   被误拒、租户库却全量放行。规则收回一处，各层只表达自己的拒绝方式
   （HTTPException / ``{"error": ...}``）。
2. 存储打标（向量/文档属于哪个租户）：写入侧与检索侧必须是同一个值，否则构建报成功、
   查询返回空 —— 最难发现的假成功。

两者的 NULL 处理**故意不同**，别改混：
- 可见性：``tenant_id`` 为 NULL 表示"没有声明过租户"，因此不按租户共享放行（fail-closed），
  与列表/详情 SQL 的 ``visibility='tenant' AND tenant_id = $n`` 同口径（NULL 匹配不上）。
- 存储打标：NULL 归一到 ``DEFAULT_TENANT_ID``，因为存量单租户数据本就只有这一个租户，
  归一后读写两侧仍然一致。
"""
from __future__ import annotations

from typing import Any

from app.config import DEFAULT_TENANT_ID


def kb_visibility_tenant(kb_row: Any) -> str:
    """KB 行声明的租户；未声明（NULL 或 SELECT 没带这列）返回空串。"""
    if "tenant_id" not in kb_row.keys():
        return ""
    return str(kb_row["tenant_id"] or "")


def kb_storage_tenant(kb_row: Any) -> str:
    """向量/文档写入与检索过滤使用的租户：NULL 归一到默认租户。"""
    return kb_visibility_tenant(kb_row) or DEFAULT_TENANT_ID


def kb_access_allowed(kb_row: Any, user_id: str, tenant_id: int = 0) -> bool:
    """自己的 / 公开的 / 同租户共享的 —— 三选一才放行。

    调用方租户缺省按默认租户比对（单租户部署网关不注入 tenant_id）。
    """
    if str(kb_row["user_id"] or "") == user_id:
        return True
    visibility = str(kb_row["visibility"] or "")
    if visibility == "public":
        return True
    return (
        visibility == "tenant"
        and kb_visibility_tenant(kb_row) == (tenant_id or DEFAULT_TENANT_ID)
    )
