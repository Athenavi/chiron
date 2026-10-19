# 核心组件：知识库（PageIndex）

> **权威来源**：`python-engine/app/knowledge/`、`pageindex-service/app/`、`app/api/knowledge.py`。
> 本页只给「链路怎么走、代码在哪、运维怎么做」。

## 架构（S4：依赖隔离）

PageIndex 跑在**独立镜像**里，原因是硬约束而非偏好：`pageindex → litellm → openai<3.0.0`
与引擎固定的 `openai 3.x` 在同一个环境里 pip 直接 `ResolutionImpossible`。

```
      ┌──────────────────────────────────────────────┐
      │ python-engine（openai 3.x，不装 PageIndex）    │
      │  /v1/kb/*  权限、DB、解密、状态机              │
      │  kb_search → HTTP                              │
      └────────┬─────────────────────────┬────────────┘
   内部 HTTP   │(X-Internal-Token)        │ 投递 kb_index
               ▼                         ▼
   ┌────────────────────────┐  ┌───────────────────────────┐
   │ pageindex-search       │  │ pageindex-worker          │
   │  无状态；chat/folder    │  │  消费 engine:tasks:kb      │
   │  索引卷（只读）          │  │  读原文 → 索引 → 回写 PG    │
   └────────────────────────┘  │  索引卷（读写）             │
                               └───────────────────────────┘
                    同一镜像 chiron/pageindex-service
```

一条知识的旅程：**上传**（引擎写原文对象 + `pending` 行）→ **Build**（引擎投递队列任务）
→ **worker 索引**（读原文 → `submit_document` → 回写 `pageindex_doc_id`/`page_count`/状态）
→ **检索**（引擎查本库已就绪 `doc_id` → HTTP 问 search → 答案 + 引用）。

## 代码在哪

```
python-engine/app/knowledge/
  remote.py           引擎侧唯一实现：RemoteProvider（HTTP 客户端 + 越界守卫兜底）
  service.py          answer_question / serialize_answer（索引已移出引擎）
  settings_store.py   用户级设置（provider + Cloud key 密文）
  crypto.py           AES-256-GCM，与 Go internal/settings 同域派生、字节级兼容
  blob.py             原文对象存储（local 共享卷 / S3）
pageindex-service/app/
  adapter.py          PageIndex 唯一出口：文件名前缀、folder 层级、越界守卫
  search_api.py       无状态检索服务（/v1/answer /v1/prepare /v1/delete /healthz）
  index_worker.py     消费 engine:tasks:kb：库级锁、索引、回写状态与 index_engine
  store.py / runtime.py / config.py
python-engine/app/queue/streams.py   队列规格（引擎队列 / kb 队列各自的 stream/组/DLQ）
```

## 隔离与信任边界

| 边界 | 机制 | 强度 |
|---|---|---|
| 跨用户 / 跨租户 | 引擎的 `kb_access_allowed`（自有 / public / 同租户）；凭据取 **KB 属主** | 强（服务端） |
| 库 ↔ 库（自托管） | 每库一个 `storage_path`；`DocStore` 只读该目录 | **强（目录即边界）** |
| 库 ↔ 库（Cloud） | 检索显式传本库 `doc_id`；引用名 `<doc_id 前缀>-<随机>_<原名>` 可验证，越界**整条拒绝** | 引擎 + 服务双道 |
| 原文访问 | **只有 worker** 能读原文（引擎上传后不读；search 不挂原文卷） | 强（部署层） |
| 账号级工具 | `agent_tools()` 不接线（它作用于用户整个 PageIndex 账号） | 强（未暴露） |

**PageIndex 的 `doc_id` / `folder_id` 是「引导」不是「限制」**（官方原文："targets at the
prompt level"）。所以隔离靠上面三条引擎/服务侧机制，folder（用户空间 → 库）只是**组织与
审计**手段。

## 升级 pageindex（checklist）

SDK 迭代以小时计（0.4.3 → 0.4.4 同日），API 面会漂移（如 0.4.4 引入 `enable_citations` /
`optimize`），索引文件格式也可能变。升级**必须**走完这几步：

1. 改 `pageindex-service/requirements.txt` 的**锁死版本**（不用 `>=`），重建镜像；
2. 跑契约测试（`pytest pageindex-service/tests/test_sdk_contract.py`）—— 它钉住我们使用的
   6 个方法与参数名（`chat(citations=...)`、`submit_document(folder_id/wait/metadata)` 等），
   漂移在 CI 就失败，而不是上线后；
3. 核对 `optimize` 等**我们显式传的参数**在新版本里的语义（它改变索引成本/耗时）；
4. 上线后比对 `knowledge_bases.index_engine` 与当前镜像版本；不一致的库用
   `POST /v1/kb/{id}/build`（body `{"force": true}`）**重建索引**（增量 build 会跳过已 ready
   的文档；不自动重建：重建要重跑 LLM，时间与费用由人决定）；
5. 灰度：先在一个知识库验证"上传 → build → 查询 → 引用"闭环。

## 部署要点

* `KNOWLEDGE_SERVICE_URL` 未配 → 知识库显示**未启用**，服务照常启动（不阻塞）；
* 索引卷（`kbindexdata`）多副本下**必须是共享卷**：search **只读**、worker 读写；
* 原文卷（`kblobdata`）只挂引擎与 worker —— search 拿不到原文；
* 内部鉴权复用 `INTERNAL_TOKEN`（fail-close：未配置时 search 拒绝一切请求）；
* 自托管的 LLM 连接由**索引侧独立配置**（`KNOWLEDGE_SELFHOSTED_LLM_*`），与在线对话的
  凭据解耦、可分别轮换。

## 延伸阅读

- [架构](architecture.md) · [安装](../getting-started/install.md) · [工具与审批](tools.md)
