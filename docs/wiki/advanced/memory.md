# 高级用法：记忆

> **权威来源**：代码（`python-engine/app/memory/` · `app/tools/memory.py`）。
> 记忆是**分层**的，先分清"哪一层"再改。

## 代码在哪

```
python-engine/app/memory/
  service.py            入口（对外的记忆服务）
  layers.py             分层定义
  manager.py            调度：什么时候写、写哪层
  summary_store.py      会话摘要
  summaries.py          摘要生成
  profile.py            用户画像
  profile_card.py       画像卡片（给模型看的那份）
  conflict_manager.py   冲突处理（新旧事实矛盾时）
python-engine/app/tools/memory.py   模型可调用的记忆工具
```

## 分层（为什么要分）

| 层 | 内容 | 特点 |
|---|---|---|
| **会话摘要** | 一段会话的压缩结果 | 随会话增长，需定期压缩 |
| **用户画像** | 跨会话的稳定事实 | 写入要谨慎，冲突要处理 |
| **事实/偏好** | 具体条目 | 可被"忘记"（有对应工具） |

## 两个真问题

1. **冲突**：新旧事实矛盾时怎么办？`conflict_manager.py` 是落点 ——
   **不要静默覆盖**，也不要把两条都留着让模型自己猜。
2. **压缩**：上下文超限时的压缩是**有损**的。压缩"为什么没发生"应该有**机器可读的码**，
   而不是只写一句日志（这是差距分析里"值得追"的一项）。

## 与 RAG 的区别（别混）

- **记忆**（本页）：关于**用户/会话**的事实，随交互积累。
- **知识库**（[RAG](../core/knowledge.md)）：**外部文档**，由用户上传，靠向量检索。

两者都"给模型喂上下文"，但**来源、生命周期与写入者完全不同**。

## 延伸阅读

- [知识库（RAG）](../core/knowledge.md) · [会话](../core/sessions.md)
- [checkpoint](checkpoints.md) —— 恢复时记忆与上下文的关系
