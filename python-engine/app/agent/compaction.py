"""上下文压缩（compaction）—— token 估算、工具结果截断与分级压缩。

从 `app.agent.runtime` 抽出（L3-4 巨型文件拆分）。本模块是**叶节点**：对
`message_codec` / `model_window` / `guards` / `media_offload` 的依赖全部是**函数内 import**
（既有的破环写法，搬迁时原样保留），因此顶层只依赖标准库，不存在循环 import。

`app.agent.runtime` 仍以 `X as X` 形式再导出这里的公开名字，既有 import 路径不变。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from app.gateway.provider import ChatMessage

logger = logging.getLogger(__name__)


# ── Token 节省参数（默认值；可被 CompactionConfig 覆盖，租户/模式可配） ──
# P2-1: 消息压缩策略调优 (生产安全检查 2026-08-17)
# 原值过于激进：MAX_CONTEXT_TOKENS=4000 (仅 GPT-4 的 1/4), MAX_MESSAGES=8
# 新值根据模型能力调整，支持租户级覆盖
MAX_CONTEXT_TOKENS = 8192  # 增加到 8K tokens (约 GPT-4 的 1/10)
MAX_MESSAGES = 16  # 增加到 16 条消息
SNIP_THRESHOLD = 0.70  # 70% 才开始压缩 (原 60%)
PRUNE_THRESHOLD = 0.85  # 85% 才裁剪旧消息 (原 80%)
TOOL_RESULT_MAX_CHARS = 16000  # 增加到 16K chars (原 8K)
TOOL_RESULT_HEAD = 2000  # head 保留长度
TOOL_RESULT_TAIL = 1000  # tail 保留长度
#: C2 段 c：历史 `tool_calls[].arguments` 的最大字符数（超则保留 JSON 骨架 + 标注截断）。
#: 0 = 不截断。老消息里的大 args（如整段文件内容）是"每轮都重发"的纯浪费。
ARG_MAX_CHARS = 2000


@dataclass(frozen=True)
class CompactionConfig:
    """历史截断策略配置（SaaS：租户/模式可手动确认方式、大小、策略）。

    对应 deepseek-harness compaction-basic 的 thresholdRatio / retainTokens /
    maxTokens 语义，参数化后由 mode_overrides.json / 租户配置注入。

    - strategy: "auto"（按阈值分级）| "snipe"（只截断长工具结果）| "prune"（丢中间消息）| "none"
    - threshold_ratio: 触发压缩的上下文压力比例（0~1），对应 deepseek thresholdRatio
    - max_messages: 消息数量硬上限（含 system）
    - max_context_tokens: 上下文预算（**0 = 用模型窗口表**）
    - tool_result_max_chars / head / tail: 工具结果截断参数
    """

    strategy: str = "auto"
    max_messages: int = MAX_MESSAGES
    #: 上下文预算（token）。**0 = 用模型窗口表**（`llm_models.context_window`，见
    #: `app/agent/model_window.py`）。C4 之前这里是硬编码的 `MAX_CONTEXT_TOKENS = 8192`，
    #: 于是所有模型共用一个预算 —— 对大窗口模型过于激进（明明还有空间就开始丢上下文），
    #: 对小窗口模型又不够安全。显式配置仍然优先（租户/模式可按自己的口径覆盖）。
    max_context_tokens: int = 0
    #: 本次压缩针对的模型名（C4）：非空时 token 估算走该模型的**校准系数**，
    #: 空则用历史近似（4 chars ≈ 1 token）。
    model: str = ""
    threshold_ratio: float = PRUNE_THRESHOLD
    snipe_ratio: float = SNIP_THRESHOLD
    tool_result_max_chars: int = TOOL_RESULT_MAX_CHARS
    tool_result_head: int = TOOL_RESULT_HEAD
    tool_result_tail: int = TOOL_RESULT_TAIL
    #: C2 段 c：历史工具调用参数的最大字符数（超长则保留 JSON 骨架 + 截断字符串值）。
    #: 0 = 不截断。老消息的大 args 会**每轮重发**，这是纯浪费。
    arg_max_chars: int = ARG_MAX_CHARS


def _to_chat_messages(messages: list[dict[str, Any]]) -> list[ChatMessage]:
    """中立格式 → gateway.ChatMessage（provider 边界，新增提供商在此适配）。"""
    from app.agent.message_codec import to_chat_messages

    return to_chat_messages(messages)


def _estimate_tokens(messages: list[dict[str, Any]], model: str = "") -> int:
    """Token 估算。

    有 `model` 时走**校准系数**（C4，`app/agent/model_window.py`）：provider 每轮回传真实
    `input_tokens`，用它反推该模型的分词密度并指数平滑 —— 比换词表（tiktoken）更准，
    且不必新增依赖（评审 01 §1.7 的三条理由）。

    无 `model` 时保持历史近似（4 chars ≈ 1 token），供不关心模型差异的调用点使用。
    """
    if model:
        from app.agent.model_window import estimator, image_tokens

        # 图片不吃字符估算（base64 的字符数说明不了它的 token 成本），单独计价后相加，
        # 否则一屏图的窗口会被算得比实际小得多、压缩迟迟不触发。
        return estimator().estimate(messages, model=model) + image_tokens(messages)

    from app.agent.model_window import strip_inline_media

    total = 0
    for m in messages:
        total += 4  # role overhead
        for v in m.values():
            if isinstance(v, str):
                total += len(v) // 4
            elif isinstance(v, list):
                # 与 model 口径同源：内联媒体不计字符，由 image_tokens 单独计价
                # （否则带一张图就是"超窗 5 倍"，见 model_window.strip_inline_media）。
                folded = json.dumps(strip_inline_media(v), ensure_ascii=False, default=str)
                total += len(folded) // 4
    # 消息间分隔符
    total += len(messages) * 2
    return total


# ── 召回序列化 ──

#: 单次召回块的最大字符数。被裁掉的那一整段可能很长，无上限会往 Redis 塞几 MB，
#: 而召回的目的是"找回关键约束与决定"，不是全文归档。
RECALL_BLOCK_MAX_CHARS = 64_000


def _serialize_for_recall(message: dict[str, Any]) -> str:
    """把一条消息序列化成可召回文本。

    **必须保留角色与工具身份**：只存 content 的话，读回来就分不清哪句是用户说的、
    哪句是模型说的、哪个是工具输出 —— 而召回的全部价值就在于"当初是谁说了什么"。

    有 `id` 时带上 `ref=`：它是 `messages.id`，也是 `read_archive` 的入参。没有这一行，
    召回块就是一段无出处的文本，模型只能整段重读，无法按需精确取回某一条。
    """
    role = str(message.get("role") or "unknown")
    header = f"## {role}"
    if message.get("id"):
        header += f" (ref={message['id']})"
    if message.get("tool_call_id"):
        header += f" (tool_call_id={message['tool_call_id']})"
    if message.get("tool_calls"):
        try:
            calls = json.dumps(message["tool_calls"], ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            calls = str(message["tool_calls"])
        header += f"\n[tool_calls] {calls}"
    content = message.get("content") or ""
    return f"{header}\n{content}" if content else header


# ── 工具结果截断（head + tail + middle indicator） ──


def _truncate_text(
    text: str,
    max_chars: int = TOOL_RESULT_MAX_CHARS,
    head_chars: int = TOOL_RESULT_HEAD,
    tail_chars: int = TOOL_RESULT_TAIL,
    *,
    recall_ref: str = "",
) -> str:
    """截断过长文本：保留 head + tail，中间用标记代替。

    `recall_ref` 非空时把取回线索写进**那条标记**而不是文本末尾 —— 因为调用方与测试都
    依赖"截断后仍以原文 tail 结尾"（`_snip_tool_results` 的既有契约），追加在尾部会破坏它。
    """
    if len(text) <= max_chars:
        return text
    head = text[:head_chars]
    tail = text[-tail_chars:]
    middle_len = len(text) - head_chars - tail_chars
    hint = f'; 原文可用 read_tool_result("{recall_ref}") 取回' if recall_ref else ""
    return f"{head}...(truncated {middle_len} chars{hint})...{tail}"


def _sanitize_tool_result(result: dict[Any, Any]) -> str:
    """工具结果 → 清洗后的文本（脱敏宿主路径 / secret）。

    压缩与截断都从这一步开始：**先进上下文的内容必须先过输出清洗**，
    否则会把宿主文件结构泄露给模型/用户。
    """
    from app.agent.guards import OutputGuard

    text = json.dumps(result, ensure_ascii=False, default=str)
    return OutputGuard(max_hits=1000).sanitize(text)


def _truncate_tool_result(
    result: dict[Any, Any], cfg: CompactionConfig | None = None
) -> str:
    """清洗 + head/tail 截断（既有行为，engine.py 等调用方仍复用）。"""
    text = _sanitize_tool_result(result)
    if cfg is not None:
        return _truncate_text(
            text, cfg.tool_result_max_chars, cfg.tool_result_head, cfg.tool_result_tail
        )
    return _truncate_text(text)


def _snip_tool_results(
    messages: list[dict[str, Any]],
    cfg: CompactionConfig | None = None,
    *,
    dropped: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Snip 阶段：压缩旧工具结果（保留 head + tail），user/assistant 消息不动。

    `dropped` 收集 `(ref, 原文)`：被压缩掉的原文不再永久丢失 —— 调用方（async 层）把它落进
    `result_store`，截断标记里附上 `read_tool_result` 线索，模型可原样取回。这**不是**新机制：
    与"单条超大结果落盘、上下文只留 `result_ref`"（`_compact_tool_result`）是同一条路径。
    """
    cfg = cfg or CompactionConfig()
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            original = msg.get("content", "") or ""
            content = original
            if len(original) > cfg.tool_result_max_chars // 2:
                # 只在真要收集时才生成 ref —— 否则会造出一个只在本轮内存里飘着的字符串
                ref = f"sn_{uuid.uuid4().hex[:12]}" if dropped is not None else ""
                content = _truncate_text(
                    original,
                    cfg.tool_result_max_chars,
                    cfg.tool_result_head,
                    cfg.tool_result_tail,
                    recall_ref=ref,
                )
                if dropped is not None and len(content) < len(original):
                    dropped.append((ref, original))
            result.append({**msg, "content": content})
        else:
            result.append(msg)
    return result


def _extractive_digest(messages: list[dict[str, Any]], max_chars: int = 1200) -> str:
    """折叠区间的**提取式**摘要：每条消息取首行，拼成要点清单。

    与 `app.context.branch_condense.extractive_summary` 同一思路（那一个是分支压缩
    的降级路径），但这里**不 import 它**：本模块是叶节点，顶层只依赖标准库，跨模块
    引用会把"压缩"和"分支压缩"两条链路绑在一起 —— 后者改签名会连累前者。

    为什么必须是提取式而不是写个占位串：`session_folds.digest` 的价值在于"事后能看出
    这段上下文在谈什么"。写 `[5 message(s) folded]` 等于只记录条数，排查时毫无信息量，
    却让字段看起来已经填过，反而掩盖了缺口。

    真正的模型摘要需要在 async 层做（本函数是同步纯函数，还被当作 `on_overflow`
    同步回调传入，接不了 await）。届时 `digest_model` 会从 `"extractive"` 变成真实
    模型名，读方据此判断摘要质量 —— 这正是该字段存在的意义。
    """
    parts: list[str] = []
    for m in messages or []:
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        first_line = content.strip().splitlines()[0][:200]
        parts.append(f"- [{m.get('role', 'user')}] {first_line}")
    if not parts:
        return ""
    return "\n".join(parts)[:max_chars]


def _prune_messages(
    messages: list[dict[str, Any]],
    cfg: CompactionConfig | None = None,
    *,
    dropped: list[tuple[str, str]] | None = None,
    folds: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Prune 阶段：不区分消息类型，保留系统提示 + 最近非系统消息

    策略：
    1. 工具结果 → 截断到 head + tail（保留可用信息，避免 AI 丢失上下文）
    2. user/assistant 配对 → 保留最近的几轮
    3. 不足时保留原始消息
    """
    cfg = cfg or CompactionConfig()
    # 先分离系统消息
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]

    # 处理工具消息：截断而非占位符（占位符会让 AI 丢失工具结果上下文）
    processed: list[dict[str, Any]] = []
    for m in other_msgs:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            original = m.get("content", "") or ""
            # 与 `_snip_tool_results` 同一套：只有真要收集时才生成 ref，被截掉的原文
            # 交给调用方落 `result_store`，截断标记里附取回线索。
            ref = f"sn_{uuid.uuid4().hex[:12]}" if dropped is not None else ""
            content = _truncate_text(
                original,
                cfg.tool_result_max_chars,
                cfg.tool_result_head,
                cfg.tool_result_tail,
                recall_ref=ref,
            )
            if dropped is not None and len(content) < len(original):
                dropped.append((ref, original))
            processed.append(
                {
                    "role": "tool",
                    "tool_call_id": m["tool_call_id"],
                    "content": content,
                    # `id` 必须跟着走：它是 `read_archive` 的 ref。在这里丢掉的话，
                    # 下面生成裁剪通知时就只有内容没有定位符，原文取不回来。
                    **({"id": m["id"]} if m.get("id") else {}),
                    # `seq` 同理：折叠区间是 `session_folds` 的坐标，丢了就算不出
                    # `[covered_from, covered_to)`。
                    **({"seq": m["seq"]} if m.get("seq") is not None else {}),
                }
            )
        else:
            processed.append(m)

    # 保留最近的消息（不超过预算）
    budget = cfg.max_messages - len(system_msgs)
    if budget <= 0:
        return system_msgs

    # 从最近位置往前取 budget 条；若 kept 中存在孤儿 tool 消息
    # （前面不是带 tool_calls 的 assistant），向前扩展到其配对的
    # assistant(tool_calls)，保证 API 配对完整、工具结果不丢失
    start = max(0, len(processed) - budget)
    # 起点本身是 tool 时，其配对的 assistant 可能在 kept 之外，先补一位
    if start < len(processed) and processed[start].get("role") == "tool" and start > 0:
        start -= 1
    while True:
        need = start
        for idx in range(start, len(processed)):
            m = processed[idx]
            if m.get("role") == "tool":
                prev_ok = (
                    idx > start
                    and processed[idx - 1].get("role") == "assistant"
                    and processed[idx - 1].get("tool_calls")
                )
                if not prev_ok:
                    # 向前找配对的 assistant(tool_calls)
                    j = idx - 1
                    while j >= 0 and not (
                        processed[j].get("role") == "assistant"
                        and processed[j].get("tool_calls")
                    ):
                        j -= 1
                    if j >= 0 and j < need:
                        need = j
        if need == start:
            break
        start = need
    kept = processed[start:]
    dropped_count = len(processed) - len(kept)
    result = system_msgs + kept

    # ── 被淘汰的消息必须可召回 ──
    #
    # 此前只有 tool 消息进 `dropped`，user/assistant 被裁掉就永久消失，只留一句
    # 计数提示。后果不是"上下文变小"，而是**正确性**问题：用户在会话开头说过的
    # 硬约束（"这个仓库绝不要动 vendor/"）会在中途无声丢失，模型此后无从查证，
    # 会重复犯同一个错。
    #
    # 现在把整段被淘汰的内容合并成**一个** ref 落盘：用户通常想恢复的是"那一段
    # 上下文"，而不是逐条挑；一个 ref 也避免了索引里塞满几十个短 ref。
    #
    # 块大小必须封顶：被裁掉的可能是几 MB 的工具输出，无上限会把 Redis 当归档盘用，
    # 而召回的目的是"找回关键约束与决定"，不是全文备份。超限时保留**靠后的**部分 ——
    # 越靠近当前回合的上下文越可能仍然生效。
    recall_ref = ""
    if dropped_count > 0 and dropped is not None:
        recall_ref = f"sn_{uuid.uuid4().hex[:12]}"
        block = "\n\n".join(_serialize_for_recall(m) for m in processed[:start])
        if len(block) > RECALL_BLOCK_MAX_CHARS:
            trimmed = block[-RECALL_BLOCK_MAX_CHARS:]
            # 从下一个消息边界开始，避免把某条消息切成半截。
            cut = trimmed.find("\n## ", 1)
            if cut > 0:
                trimmed = trimmed[cut + 1:]
            omitted = len(block) - len(trimmed)
            block = (
                f"[{omitted} chars of earlier context omitted from this recall block]\n\n{trimmed}"
            )
        if block:
            dropped.append((recall_ref, block))
        else:
            recall_ref = ""

    # 被裁掉区间的 `messages.id` 列表：这是 `read_archive` 真正能查的 ref。
    # 只给 `sn_xxx`（Redis `result_store`，2 小时 TTL）是不够的 —— 会话过夜再回来，
    # 那个 ref 已经过期，模型拿着一条永远取不回的线索。
    covered_ids = [str(m["id"]) for m in processed[:start] if m.get("id")]
    # 折叠区间用 `messages.seq`（半开区间）。`processed` 里若有消息缺 seq
    # （例如 Go 直接注入、未从 PG 读回），区间就是不可信的 —— 宁可整条不记，
    # 也不要写一个会误覆盖别的消息的区间。
    covered_seqs = [m["seq"] for m in processed[:start] if m.get("seq") is not None]
    seq_range_ok = bool(covered_seqs) and len(covered_seqs) == start

    if dropped_count > 0:
        # 给**模型**留痕：被淘汰的消息不能无声消失。此前只有 `compaction` 事件告诉**前端**
        # "压缩了"，模型侧完全看不到自己少了多少上文 —— 它只会觉得自己记错了。
        # 用 user 角色而不是 system：`_apply_system_prefix` 会把所有 system 消息摘掉重排，
        # 放在 system 段里会被它吞掉。连续 user 消息是既有形态（`merge_user_message` 就处理这个）。
        note = f"[{dropped_count} earlier message(s) were pruned to fit the context window]"
        if covered_ids:
            # 持久 ref 优先：它没有 TTL，是真正的归档入口。
            # 只列前若干个 —— 一次裁掉几十条时全列会把通知本身撑成一大段上下文。
            sample = ", ".join(f'"{i}"' for i in covered_ids[:10])
            more = (
                f" (and {len(covered_ids) - 10} more)"
                if len(covered_ids) > 10
                else ""
            )
            note = (
                f"{dropped_count} earlier message(s) were pruned to fit the context window. "
                f"`read_archive(ref=..., offset=0, limit=8000)` returns their full text — "
                f"use it instead of assuming they never happened. refs: {sample}{more}"
            )
        elif recall_ref:
            # 无持久 id 时的回落：老会话（0016 之前写入）或 Go 传进来的消息没有 id。
            note = (
                f"{dropped_count} earlier message(s) were pruned to fit the context window. "
                f"`read_tool_result(result_ref=\"{recall_ref}\", offset=0, limit=8000)` returns "
                f"their full text — use it instead of assuming they never happened."
            )
        result.insert(len(system_msgs), {"role": "user", "content": note})

    # ── 折叠索引：把「哪段 seq 被折叠了」记下来 ──
    #
    # `messages` 一字节不动（压缩只影响本次请求的组装），但没有这行记录，
    # 库里就无从判断"这段原文是被哪次摘要覆盖而离开上下文的"。跨副本重建
    # 上下文时也依赖它才知道该拼多少条原样尾部（body_suffix）。
    if folds is not None and dropped_count > 0 and seq_range_ok:
        folds.append(
            {
                # 半开区间 [min, max+1)：seq 单调，所以首尾就够定界。
                "covered_from": int(min(covered_seqs)),
                "covered_to": int(max(covered_seqs)) + 1,
                "covered_ids": covered_ids,
                # 折叠发生**之后**仍在上下文里的尾部条数。投影 =
                # [冻结体][活动摘要][原样尾部]，缺了它拼接就会错位。
                "body_suffix": len(kept),
                # 摘要 + 来源标注。**这里不调模型**：`_prune_messages` 是同步纯函数，
                # 还被当作 `on_overflow` 同步回调传入，接不了 await。写入的是一条
                # 占位摘要会误导后续读者，所以复用 `branch_condense` 里那条提取式
                # 实现（纯函数、只依赖标准库），并由 `digest_model` 如实标注来源。
                "digest": _extractive_digest(processed[:start]),
                "digest_model": "extractive",
                "digest_prompt": "prune",
            }
        )

    logger.info(
        "Prune: %d → %d messages (dropped %d old)",
        len(messages),
        len(result),
        dropped_count,
    )
    return result


def _apply_folds(
    messages: list[dict[str, Any]],
    folds: list[dict[str, Any]],
    *,
    model: str = "",
) -> list[dict[str, Any]] | None:
    """把已折叠区间换成摘要，返回投影 `[冻结体(系统提示)][活动摘要][原样尾部]`。

    **返回 `None` 表示「投影不可用，调用方应回退全量」**——宁可超预算走实时裁剪，
    也不发一份漏了某段上下文却看起来完整的投影。

    为什么校验按**位置**而不按算术：`messages.seq` 是整表
    `GENERATED ALWAYS AS IDENTITY`，多会话并发写入会让同一个 session 内的 seq
    **不连续**（被别的会话的行隔开）。所以 `max_seq - body_suffix == covered_to`
    这类等式不成立，必须按排序后的位置关系校验。

    校验三条，任一不成立即返回 None：
    1. 每条非 system 消息都有 `seq` —— 没有坐标就无法定位折叠边界；
    2. 相邻 fold 首尾相接（`from[i] == to[i-1]`）—— 断档意味着有消息既没被摘要
       也没留在尾部，会凭空消失；
    3. 尾部条数不少于最后一次折叠记录的 `body_suffix` —— 该字段是当时留下的
       原样条数，少于它说明数据比记录时还少，投影不可信。
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if not rest or not folds:
        return None

    # 校验 1：坐标完备
    if any(m.get("seq") is None for m in rest):
        return None

    ordered = sorted(rest, key=lambda m: int(m["seq"]))
    ordered_folds = sorted(folds, key=lambda f: int(f["covered_from"]))

    # 校验 2：首尾相接
    for prev, cur in zip(ordered_folds, ordered_folds[1:], strict=False):
        if int(cur["covered_from"]) != int(prev["covered_to"]):
            return None

    last_to = int(ordered_folds[-1]["covered_to"])
    tail = [m for m in ordered if int(m["seq"]) >= last_to]

    # 校验 3：尾部不少于记录时的条数
    if len(tail) < int(ordered_folds[-1].get("body_suffix") or 0):
        return None

    # 折叠边界之前的消息必须**全部**落在某个 fold 区间内。
    # 否则那段消息既没进摘要、也没进尾部 —— 投影看起来完整实则丢内容。
    # （先前这里写的是 `len(ordered) - len(tail)`，那是恒等式，等于没校验。）
    def _covered_by_fold(seq: int) -> bool:
        return any(int(f["covered_from"]) <= seq < int(f["covered_to"]) for f in ordered_folds)

    if any(not _covered_by_fold(int(m["seq"])) for m in ordered if int(m["seq"]) < last_to):
        return None

    digests = [f["digest"] for f in ordered_folds if f.get("digest")]
    if not digests:
        return None

    # 摘要用 user 角色：`_apply_system_prefix` 会把 system 段摘掉重排，
    # 放在 system 里会被它吞掉（与裁剪通知同一个理由）。
    summary: dict[str, Any] = {
        "role": "user",
        "content": (
            "[以下是本会话更早上下文的摘要。需要原文时用 "
            "read_archive(ref=..., offset=0, limit=8000) 按 id 取回]\n\n"
            + "\n\n".join(digests)
        ),
    }

    projected = system_msgs + [summary] + tail

    # 投影必须真的更省，否则用全量（避免摘要质量差时反而变长）
    if model and _estimate_tokens(projected, model=model) >= _estimate_tokens(
        messages, model=model
    ):
        return None
    return projected


def _compact_messages(
    messages: list[dict[str, Any]],
    cfg: CompactionConfig | None = None,
    *,
    dropped: list[tuple[str, str]] | None = None,
    folds: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """分级压缩：根据配置的策略、上下文压力和消息数量选择压缩方式

    策略（cfg.strategy）：
    - "none"   → 不压缩
    - "snipe"  → 只截断长工具结果（保留全部消息）
    - "prune"  → 只裁剪旧消息（保留系统 + 最近 N 条）
    - "auto"   → 默认：按阈值分级（SNIP → PRUNE）+ 消息数硬上限
    """
    cfg = cfg or CompactionConfig()
    if cfg.strategy == "none":
        return messages

    from app.agent.media_offload import truncate_tool_call_arguments

    # C2 段 c：先截断历史工具调用参数（保留 JSON 骨架 + 标注）。放在 `strategy` 判定之后，
    # 是因为"显式声明不压缩"应当被尊重；而它本身不是压缩 —— 是"别把明知没用的字节反复发出去"。
    messages = truncate_tool_call_arguments(messages, cfg)
    if cfg.strategy == "snipe":
        return _snip_tool_results(messages, cfg, dropped=dropped)
    if cfg.strategy == "prune":
        return _prune_messages(messages, cfg, dropped=dropped, folds=folds)

    # auto：硬上限（无论 token 使用率多少，消息数不能超过 cfg.max_messages）
    if len(messages) > cfg.max_messages:
        return _prune_messages(messages, cfg, dropped=dropped, folds=folds)

    # Token-based 压缩
    tokens = _estimate_tokens(messages, cfg.model)
    ratio = tokens / cfg.max_context_tokens if cfg.max_context_tokens else 0

    if ratio >= cfg.threshold_ratio:
        logger.info(
            "Compact PRUNE at %.0f%% (%d/%d tokens)",
            ratio * 100,
            tokens,
            cfg.max_context_tokens,
        )
        return _prune_messages(messages, cfg, dropped=dropped, folds=folds)
    elif ratio >= cfg.snipe_ratio:
        logger.info(
            "Compact SNIP at %.0f%% (%d/%d tokens)",
            ratio * 100,
            tokens,
            cfg.max_context_tokens,
        )
        return _snip_tool_results(messages, cfg, dropped=dropped)

    return messages  # 无需压缩


def _ensure_valid_tool_sequence(
    messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """修复消息序列中的工具调用配对，保证 OpenAI/DeepSeek API 兼容。

    双向修复：
    1. 孤立 tool 消息（无前导 assistant(tool_calls)）→ 删除
    2. **部分配对的 assistant(tool_calls)**（中断/审批等待时缓存里
       assistant([c1,c2]) 只有 tool(c1)，缺 c2）→ 从未配对的 id 从
       assistant.tool_calls 中移除（保留 content），否则 API 报
       "assistant message with tool_calls must be followed by tool messages"
    """
    result: list[dict[str, Any]] = []
    pending_tc: dict[str, Any] | None = None  # 暂存的 assistant(tool_calls)
    pending_ids: set[str] = set()  # 其声明的 tool_call_id
    answered_ids: set[str] = set()  # 已收到 tool 结果的 id
    pending_tools: list[dict[str, Any]] = []  # 匹配的 tool 消息（flush 时排在 assistant 后）

    def _flush_pending() -> None:
        nonlocal pending_tc
        if pending_tc is None:
            return
        missing = pending_ids - answered_ids
        if missing:
            logger.warning(
                "Dropping unmatched tool_calls %s (no tool result)", sorted(missing)
            )
            pending_tc["tool_calls"] = [
                tc for tc in pending_tc["tool_calls"] if tc.get("id") in answered_ids
            ]
        result.append(pending_tc)
        result.extend(pending_tools)
        pending_tc = None
        pending_tools.clear()

    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            _flush_pending()
            pending_tc = dict(msg)
            pending_ids = {cast(str, tc.get("id")) for tc in msg["tool_calls"]}
            answered_ids = set()
            pending_tools = []
            continue
        if (
            role == "tool"
            and pending_tc is not None
            and msg.get("tool_call_id") in pending_ids
        ):
            answered_ids.add(cast(str, msg.get("tool_call_id")))
            pending_tools.append(msg)
            continue
        if role == "tool":
            # 孤立 tool 消息（无前导 assistant(tool_calls)）
            logger.warning(
                "Dropping orphan tool message (no preceding assistant with tool_calls)"
            )
            continue
        _flush_pending()
        result.append(msg)
    _flush_pending()
    return result
