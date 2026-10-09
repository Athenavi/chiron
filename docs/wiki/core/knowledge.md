# 核心组件：知识库（RAG）

> **权威来源**：代码本身（`python-engine/app/rag/` · `app/knowledge/`）+ `README.md` §关键配置。
> 本页只给「链路怎么走、代码在哪」。

## 一条知识从上传到被引用

```
上传（前端分片） ─► 对象存储(MinIO/S3) ─► 解析(parser) ─► 切块与向量化(builder)
                                                              │
                                                              ▼
                                                         Milvus（向量）
                                                              │
用户提问 ─► 检索(retriever / hybrid_search) ◄─────────────────┘
                    │
                    ▼
            上下文注入(context_injector) ─► 交给 agent 循环
```

## 代码在哪

```
python-engine/app/rag/
  builder.py            825 行 —— 入库：切块、向量化、写向量库
  parser.py             250 行 —— 文档解析
  hybrid_search.py      246 行 —— 混合检索
  retriever.py          210 行 —— 检索入口
  context_injector.py   240 行 —— 把命中片段注入上下文
python-engine/app/knowledge/   知识库领域逻辑（库/文档/权限）
frontend-vue/src/utils/uploader.ts   分片上传（2MB 分片、并发 3、断点续传、失败退避重试）
frontend-vue/src/views/KnowledgeView.vue 等   前端入口
```

## 三个存储各司其职

| 存储 | 存什么 |
|---|---|
| **PostgreSQL** | 知识库/文档的**元数据**与权限 |
| **Milvus** | **向量**与检索 |
| **MinIO / S3** | **原文**（对象存储；对外给签名 URL） |

## 两个实现细节（踩过坑的）

1. **分片上传的指纹必须能区分"同名同大小"的文件** —— 指纹含 `lastModified` 与分片大小；
   只用 `name+size` 会让导出文件/重复下载**复用同一个 `upload_id`**，把 A 的分片写进 B 的会话。
2. **断点续传要跳过服务端已收的分片** —— 否则"续传"等于重传。

## 延伸阅读

- [架构](architecture.md) —— 存储栈的位置
- [安装](../getting-started/install.md) —— Milvus / MinIO 的依赖与起法
- [工具与审批](tools.md) —— 检索类工具在工具面上的位置
