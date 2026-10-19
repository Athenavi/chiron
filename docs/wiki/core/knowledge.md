# 核心组件：知识库（PageIndex）

> **权威来源**：代码本身（`python-engine/app/knowledge/` + `app/api/knowledge.py`）+ `README.md` §关键配置。
> 本页只给「链路怎么走、代码在哪」。

## 一条知识从上传到被引用

```
上传（前端 base64） ─► 对象存储（S3 / 共享卷） ─► knowledge_documents(pending)
                                                        │
                                          POST /v1/kb/{id}/build  →  队列任务 kb_index
                                                        │
                                                        ▼
                        provider.submit_document：解析 → 建树索引 → 存到 provider 侧
                                                        │
用户提问 ─► provider.answer（在树上推理） ◄──────────────┘
                    │
                    ▼
            答案 + 引用（页 / 块） ─► POST /v1/kb/{id}/query、agent 工具 kb_search
```

与旧实现（切块 + 向量 + 全文检索 + RRF）相比的**四处语义变化**：

1. **没有"片段"这个概念**：PageIndex 是推理式检索，返回的是答案与可追溯引用（页码 / 块）。
2. **上传 ≠ 索引**：上传只把原文落到对象存储并记一行 `pending`；索引是队列任务 `kb_index`。
3. **provider 二选一**：每个用户只能选自托管（PageIndex local）或 PageIndex Cloud，见下。
4. **没有向量库**：Milvus / pgvector 不再参与知识库（Milvus 只剩记忆层的可选后端）。

## 代码在哪

```
python-engine/app/knowledge/
  providers/base.py        provider 协议 + 数据类（ProviderContext / AnswerResult / Citation）
  providers/selfhosted.py  PageIndex local：每个知识库一个 storage_path，仅 PDF
  providers/cloud.py       PageIndex Cloud：用户自带 API key，支持 folder/metadata
  providers/registry.py    名字 → 实例；按用户解析可用 provider + 健康探测
  service.py               业务编排（索引、问答、状态回写）—— API 与队列共用
  settings_store.py        用户级设置（provider + Cloud key 密文）
  crypto.py                AES-256-GCM，与 Go 侧 internal/settings 同域派生、字节级兼容
  blob.py                  原文对象存储（local 共享卷 / S3）
  llm.py                   模型名 → litellm 目标（复用 app/providers 的端点与 key）
python-engine/app/api/knowledge.py     /v1/kb/*  HTTP 面
python-engine/app/tools/kb.py          agent 工具：kb_list / kb_search
frontend-vue/src/views/Knowledge*.vue  列表 / 详情 / 设置
```

## 两种 provider

| | 自托管（`selfhosted`） | Cloud（`cloud`） |
|---|---|---|
| 索引与存储 | 本地目录（**必须落在所有副本可见的共享存储上**） | PageIndex 托管 |
| 支持的文档 | 仅 text-based PDF | PDF / PPTX / Word（含扫描件 OCR） |
| 引用粒度 | 页 | 块 |
| 依赖 | 部署方配置的 LLM（index + chat 两个模型） | 用户自己的 PageIndex API key + 我们的 chat 模型 |

**二选一是产品约束**（`user_knowledge_settings.provider`）：知识库的 `provider` 必须与用户当前
设置一致，否则 409 —— 否则会出现"库建在自托管、用户却切到 Cloud"，表现为"传了文档却查不到"。
已有知识库时不允许直接切换 provider（要先删除或重建）。

## 三个实现细节（会真出问题的）

1. **每个知识库一个 `storage_path`**。PageIndex local 的 `browse_documents` 会列出 storage 里的
   **全部**文档；所有库共用一个目录就会跨库、乃至跨租户列出别人的文档。
2. **多实例必须共享索引目录**。local 模式没有中心服务，索引文件就是唯一真相；放在 Pod 本地盘上
   会表现为"同一份文档时而能查、时而查不到"。同一 KB 的索引任务由队列按库串行化（`kb_index`）。
3. **索引任务不乐观置成功**。文档状态是 `pending → indexing → ready/error`，只有真正跑完才回写。

## 隔离与信任边界（重要）

| 边界 | 机制 | 强度 |
|---|---|---|
| 跨用户 / 跨租户 | API 与 agent 工具都过 `kb_access_allowed`（自有 / public / 同租户）；provider 凭据取 **KB 属主** | 强（服务端校验） |
| 库 ↔ 库（自托管） | 每个库一个 `storage_path`，PageIndex 的 `DocStore` 只读该目录 | **强（目录即边界）** |
| 库 ↔ 库（Cloud） | 检索时显式传本库已就绪文档的 `doc_id`；引用名 `<doc_id 前缀>-<随机>_<原名>` 可**验证**归属，越界即拒绝返回 | 引擎侧兜底，见下 |

**PageIndex 的 `doc_id` / `folder_id` 是「引导」不是「限制」**（官方文档原话：
context "does not restrict tool access"）。所以 Cloud 模式的加固有三条，全在引擎侧：

1. 库里没有已就绪文档 → **拒绝检索**，绝不放宽到整个账号；
2. 引用必须能映射回本库文档，越界 → **整条拒绝返回**（`chat_result.guard_citations`）——
   只删引用不行：答案正文可能已经写进了范围外内容；
3. 账号级的浏览工具（`agent_tools`）**不接线**给 agent —— 它作用于整个账号。

Cloud 模式下 **PageIndex 账号本身就是信任边界**：同一个账号里不要放不该被检索到的文档。
Chiron 把上传的文档放进**以 user_id 命名的 folder**（其下按知识库再分一层），便于识别与
审计 —— 但这是组织手段，**不是**安全边界。

## 延伸阅读

- [架构](architecture.md) —— 存储栈的位置
- [安装](../getting-started/install.md) —— S3 / 共享存储的依赖与起法
- [工具与审批](tools.md) —— 检索类工具在工具面上的位置
