"""知识库领域异常。

分开定义而不是复用 ``ValueError``：API 层要把"用户输入不合法"（400）、"provider 不可用"
（503）与"上游失败"（502）区分开，否则前端只能一律显示"操作失败"。
"""

from __future__ import annotations


class KnowledgeError(Exception):
    """知识库领域错误基类。"""


class ProviderUnavailable(KnowledgeError):
    """provider 未配置 / 未启用（对应 HTTP 503，且消息面向用户可读）。"""


class ProviderFailed(KnowledgeError):
    """provider 调用失败（凭据错误、上游不可达、索引失败等）。"""


class UnsupportedDocument(KnowledgeError):
    """当前 provider 不支持该文档类型。

    local（自托管）只吃 text-based PDF —— 这是 PageIndex 的既定边界，不是实现取舍；
    Cloud 支持 PDF/PPTX/Word。所以"能不能上传"必须先问 provider。
    """


class BlobMissing(KnowledgeError):
    """原文对象不存在（对象存储被清空/迁移不完整）。"""
