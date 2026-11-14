"""原始目标 + 意图：每轮注入消息尾部的「目标提醒」。

为什么放在消息尾部，而不是 system 段（三个理由都是硬约束）：

1. **前缀缓存**。前缀缓存按"逐字相同的最长前缀"命中。目标与计划每轮可能变
   （todo 一推进就变），塞进 system 段等于从那条消息起整段失效 —— 长任务里
   每个回合都要重算一次完整提示词。放尾部，稳定前缀逐字不变。
2. **压缩**。`compaction._prune_messages` 会截断 tool 消息、淘汰更旧的整段。
   尾部提醒在压缩**之后**才拼上（见 `agent.runtime` 主循环），天然不在压缩
   作用范围内，不必给压缩器开后门。
3. **累积**。`messages` 跨轮复用，尾部提醒每轮重拼 ⇒ 必须先摘掉上一份
   （`strip_reminder`），否则同一段文字会随轮次越堆越多。

意图识别是**确定性关键词规则，不调 LLM**：每轮一次模型调用只为分类，延迟与
成本翻倍，而收益只是换个措辞。任务拆解不在这里做 —— 它由 `todo_write` 落在
taskboard 上，本模块只负责把它渲染进同一块提醒，让模型每轮都看见"原始要什么
／拆成哪几步／走到哪了"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 尾部提醒的起始标记。用它识别"这份是提醒、不是用户真说的话"，
#: 下一轮据此把它摘掉（也避免 `extract_original_goal` 把自己当成原始请求）。
REMINDER_MARK = "<goal-reminder>"

#: 原始请求进上下文的字符上限。超长时保头保尾 —— 约束在头，"不要做什么"
#: 与验收条件常在尾，中间的过程性描述最不关键。
GOAL_MAX_CHARS = 2000

#: 意图标签 → 中文说明。顺序即优先级（同分时取靠前的）。
_INTENT_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("fix", "修复缺陷", ("修复", "报错", "错误", "失败", "崩溃", "异常", "不工作", "排查",
                        "fix", "bug", "error", "traceback", "broken")),
    ("build", "实现功能", ("实现", "新增", "开发", "支持", "加一个", "做一个",
                          "implement", "feature", "add ", "build ")),
    ("refactor", "重构整理", ("重构", "拆分", "提取", "重写", "整理", "下沉",
                             "refactor", "cleanup", "rename", "extract")),
    ("review", "评审审查", ("审查", "评审", "复盘", "检查代码", "有没有问题",
                           "review", "audit", "lint")),
    ("research", "调研分析", ("调研", "对比", "分析", "为什么", "原理", "评估",
                             "research", "compare", "why ", "explain")),
    ("ops", "部署运维", ("部署", "发布", "上线", "配置", "迁移", "运维",
                        "deploy", "release", "configure", "migrate")),
    ("docs", "文档写作", ("文档", "注释", "readme", "写清楚", "说明",
                         "doc", "document", "comment")),
)

_INTENT_LABELS = {key: label for key, label, _ in _INTENT_RULES}
_UNKNOWN_INTENT = "未归类"


def _text_of(content: Any) -> str:
    """把消息 content（字符串或 parts 数组）压成纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "\n".join(p.strip() for p in parts if p.strip()).strip()
    return str(content or "").strip()


def clip(text: str, limit: int = GOAL_MAX_CHARS) -> str:
    """超长文本保头保尾地截断（中间用省略号示意）。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = max(1, limit - head - 4)
    return f"{text[:head]}\n……\n{text[-tail:]}"


def classify(text: str) -> tuple[str, tuple[str, ...]]:
    """确定性意图识别：返回 (标签, 命中的关键词)。

    同分取 `_INTENT_RULES` 里靠前者；一个都没命中返回 `("", ())` ——
    调用方据此**不写意图行**，而不是硬安一个"未归类"上去（那是噪声）。
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return "", ()
    best_key = ""
    best_hits: tuple[str, ...] = ()
    for key, _label, keywords in _INTENT_RULES:
        hits = tuple(k for k in keywords if k in lowered)
        if len(hits) > len(best_hits):
            best_key, best_hits = key, hits
    return best_key, best_hits


def label_of(key: str) -> str:
    """意图标签 → 中文说明。未知标签返回 `_UNKNOWN_INTENT`。"""
    return _INTENT_LABELS.get(key, _UNKNOWN_INTENT)


def extract_original_goal(messages: list[dict[str, Any]], task: Any) -> str:
    """取会话的**原始请求**：历史里第一条真正的 user 消息。

    跳过提醒本身（`REMINDER_MARK`）与空消息；历史里一条都没有时回落到
    `task.content`（新会话的正常形态）。取不到返回空串 ⇒ 调用方不注入，
    与引入本模块前逐字相同。
    """
    for message in messages:
        if message.get("role") != "user":
            continue
        text = _text_of(message.get("content"))
        if not text or text.startswith(REMINDER_MARK):
            continue
        return clip(text)
    return clip(_text_of(getattr(task, "content", "")))


def is_reminder(message: dict[str, Any]) -> bool:
    """这条消息是不是上一轮拼上去的提醒。"""
    return message.get("role") == "user" and _text_of(
        message.get("content")
    ).startswith(REMINDER_MARK)


def strip_reminder(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """摘掉上一轮的提醒（幂等）。"""
    return [m for m in messages if not is_reminder(m)]


@dataclass(frozen=True)
class GoalContext:
    """一次 run 的目标现场（**只在 run 开始时算一次**）。"""

    text: str
    intent: str = ""
    hits: tuple[str, ...] = ()

    @property
    def present(self) -> bool:
        return bool(self.text)

    def render(self, plan_block: str = "") -> str:
        """渲染尾部提醒。`plan_block` 为空 ⇒ 提示模型先做拆解。"""
        if not self.present:
            return ""
        lines = [REMINDER_MARK, "你的**原始目标**（本次会话用户最初的要求，不要偏离）：", self.text, ""]
        if self.intent:
            hits = "、".join(self.hits[:4])
            lines.append(f"识别到的意图：{label_of(self.intent)}" + (f"（线索：{hits}）" if hits else ""))
        if plan_block:
            lines.append("")
            lines.append("当前拆解与进度（来自你自己维护的 todo 清单）：")
            lines.append(plan_block)
            lines.append("")
            lines.append(
                "每完成一步就用 todo_write 更新状态；清单与原始目标不符时，先改清单再动手。"
            )
        else:
            lines.append("")
            lines.append(
                "你还没有拆解这个目标。**先**用 todo_write 把它拆成 3-8 个可执行步骤，"
                "再逐步推进 —— 不要跳过拆解直接动手。"
            )
        # 委派判断：只在真的"可并行 / 上下文过重"时才值得开子 agent。
        # 无条件鼓励委派会显著抬高成本（每个子 agent 都是独立会话）。
        lines.append("")
        lines.append(
            "委派判断：若某个步骤可独立完成且会灌进大量无关上下文（大范围检索、"
            "多文件审查、多方案对比），用 subagent 把它隔离掉，只取回结论；"
            "步骤之间强依赖、或只需一两次工具调用时，自己做，不要委派。"
        )
        lines.append("</goal-reminder>")
        return "\n".join(lines)


def build(messages: list[dict[str, Any]], task: Any) -> GoalContext:
    """从历史里提炼目标现场。"""
    text = extract_original_goal(messages, task)
    key, hits = classify(text)
    return GoalContext(text=text, intent=key, hits=hits)


def with_reminder(messages: list[dict[str, Any]], block: str) -> list[dict[str, Any]]:
    """把提醒拼到消息尾部（先摘旧的，保证跨轮不累积）。

    用 `role="user"` 而非 `role="system"`：多家 provider 对"会话中途出现 system
    消息"处理不一（Anthropic 会把它抽出去并入顶层 system，位置信息丢失），
    而 user 消息在三种编码下都按原位置保留。
    """
    cleaned = strip_reminder(messages)
    if not block:
        return cleaned
    return [*cleaned, {"role": "user", "content": block}]
