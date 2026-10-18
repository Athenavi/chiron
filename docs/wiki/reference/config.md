# 参考：配置与环境变量

> **权威来源**：仓库根 **`.env.example`** + `docker-compose.yml` 里的 `${VAR:?…}` 声明。
> **有门禁**：`scripts/check_compose_env.py` —— compose 里声明的**必填**变量必须在 `.env.example` 出现。

## 两个来源，一处权威

| 来源 | 作用 |
|---|---|
| `.env.example` | 变量**清单与说明**（`APP_SECRET`、`CHIRON_DATA_DIR` 等） |
| `docker-compose.yml` 的 `${VAR:?…}` | 声明**哪些是必填**（缺了 compose 直接报错） |

**门禁保证两者不漂移** —— 所以想知道"到底有哪些变量"，**读这两个文件**，不要读本页。

## 初始化与自检

```bash
python scripts/init.py            # 交互式：生成 APP_SECRET、写 .env、自检 PostgreSQL/Redis
python scripts/init.py --check    # 只自检：必填项 + 连通性
```

## 几个容易配错的

| 变量 | 坑 |
|---|---|
| `REDIS_ADDR` | **网关读这个**（`host:port`），不是 `REDIS_URL` |
| `REDIS_URL` | 引擎与测试用（`redis://host:port/db`）—— 两个格式不同，别互抄 |
| `DATABASE_DSN` | 迁移与应用都要；**库不在 compose 内**，由云厂商/DBA 维护 |
| `CHIRON_BUILTIN_SKILLS_PATH` | 内置技能目录；**容器部署必须挂载 `market/skills/`**，缺了会告警且技能列表为空 |
| `MCP_POOL_ENABLED` / `MCP_MAX_*` / `MCP_OWNER_LEASE_ENABLED` | MCP 连接池与 owner lease（多租户连接经济学） |
| `MEMORY_VECTOR_BACKEND` | 只管 **L3 摘要向量**存哪 / 怎么扫（`pg` 默认，可配 `milvus`）；**不是** RAG 的 `VECTOR_DB_TYPE`（那个决定知识库向量存哪），别互相抄值。切换 = 改配置 + 重启，后台「系统设置」里没有这一项 |
| `MEMORY_CONSOLIDATE_BATCH` | L3 巩固的节拍：**每 N 回合入队一次**，会话结束的 `memory_rollup` 覆盖不足一批的尾巴。M14 之前这条配置全库无人引用（实际每回合同步整段会话）；`<=1` 是"每回合都巩固"，不是"关掉" |
| `MEMORY_SUMMARISER_*` | 摘要正文由 LLM 生成（`_ENABLED` **默认开**、`_MODEL` 空则复用 `DEFAULT_MODEL`、`_MAX_TOKENS`/`_MAX_INPUT_CHARS` 是两次上限）。与 C6 提炼（默认关）相反是有意的：提炼每回合多一次调用、成本随对话量线性涨，巩固按批走且正文会被长期召回。关掉后正文退回"每条消息前 200 字符"的抽取式拼接——那不是你想要的东西，除非在为一个没有 embedding 服务的部署兜底 |
| `CHIRON_TEST_POSTGRES_DSN` / `CHIRON_TEST_REDIS_ADDR` | **只给测试用**：置上它们才会跑真实栈的 live 子集 |

## 校验配置本身

```bash
docker compose config --quiet     # compose 语法与变量插值校验
```

## 延伸阅读

- [部署](../production/deploy.md) · [安装](../getting-started/install.md)
- [多实例](../production/multi-instance.md) —— 多副本下必须配对的变量
