"""子 Agent 结果入库前的敏感信息扫描与脱敏（D6 决策：默认扫描+脱敏，可选列加密）。

为什么单独做：子 Agent 的价值在于"主动读文件"，因此它的 L0 完整过程里出现密钥的
概率远高于普通对话（``.env``、CI 配置、证书、``kubeconfig`` …）。这些内容一旦落库，
会通过备份、管理端查询、审计导出扩散出去。

策略（见 docs/subagent-design.md §7）：
* 只匹配**明确的密钥形态**，避免误伤正文（宁可漏报，不做正则大扫除）；
* 命中处替换为 ``[REDACTED:<kind>]``，并返回命中计数写入 ``subagent_runs.redacted_count``；
* 强要求部署可另开 ``SUBAGENT_STEPS_ENCRYPT`` 对 ``content`` 列做列级加密（复用
  internal/settings 的 AES-256-GCM 机制），本模块只负责脱敏层。
"""
from __future__ import annotations

import re
from typing import Any

# 命中替换模板
REDACTED_TEMPLATE = "[REDACTED:{kind}]"

# 保守的密钥形态清单（顺序即匹配顺序）
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    )),
    ("password_assignment", re.compile(
        r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*[\"']?([^\s\"',]{8,})"
    )),
]


def redact_text(text: str | None) -> tuple[str, int]:
    """脱敏 *text*，返回 ``(脱敏后文本, 命中数)``。

    非字符串/空值原样返回、计数为 0 —— 调用方无需先判空。
    """
    if not text or not isinstance(text, str):
        return text or "", 0
    hits = 0
    out = text
    for kind, pattern in PATTERNS:
        out, replaced = pattern.subn(REDACTED_TEMPLATE.format(kind=kind), out)
        hits += replaced
    return out, hits


def redact_payload(value: Any) -> Any:
    """递归脱敏 payload（dict/list/str），返回 ``(脱敏后对象, 命中数)``。

    用于 task / artifacts 等结构化字段（只处理字符串叶子，其它类型原样保留）。
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        total = 0
        out: dict[Any, Any] = {}
        for k, v in value.items():
            out[k], n = redact_payload(v)
            total += n
        return out, total
    if isinstance(value, list):
        total = 0
        out_list: list[Any] = []
        for item in value:
            redacted, n = redact_payload(item)
            out_list.append(redacted)
            total += n
        return out_list, total
    return value, 0
