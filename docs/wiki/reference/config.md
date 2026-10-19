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
| `AGENT_MAX_STEPS/TURNS/TOKENS/SECONDS/COST` | **只有引擎读**，是"一个 run 全程"的预算（每轴 0 = 不限；主 Agent tokens 默认 400k、steps 60）。⚠ 网关 `config/config.go` 里有**同名的 `AGENT_MAX_TOKENS`（8192，含义是"单次调用的 max output tokens"）与 `AGENT_MAX_TURNS`（10）**，连同 `AGENT_CONTEXT_LIMIT`（20）这一簇**都没有任何读取点**——`cmd/chiron/main.go` 只把后台「系统设置」的 `agent.max_turns/max_tokens/context_limit` 写进这三个字段，写了不读 ⇒ 想调 run 级预算只设引擎侧这一套，别以为改一个变量两侧一起动。`AGENT_MAX_SECONDS` 默认不限（回合超时归网关），只有显式设了它，"崩溃接管那一段把上一段用时接着算"才会生效 |
| `SUBAGENT_MAX_*` / `SUBAGENT_LINE_MAX_TOKENS` | 前者是**单个子 run** 的预算（tokens 默认 200k）；后者是**整条任务线**的上限（默认 2M，账本单位是血缘闭包 `root_run_id`：同一 root 派生的全部委派与**反复续跑**一起算）。两道闸门语义不同——per-run 拦"一个 run 失控"，线账拦"一条线被续跑 N 次 = N 份额度"。线账耗尽 ⇒ 暂停该线并挂出「提额」待办，**不是**静默失败；单条线可由 `run_lineage_ledger.cap_tokens` 覆盖（`-1` 不限、`0` 跟随默认）。预留键在 Redis（单条约 1h 自然到期），账本真相在 PG |
| `SUBAGENT_LINE_PROJECTION` | 在途预留的**重述**开关（`off` / `shadow` 默认 / `enforce`）：派发时那条预留是 per-run 上限的静态猜值，开了之后每个"带用量"的事件把它换成 `已花 + 观测速率×120s 投影余量`（夹在 per-run 上限与已花之间；没样本原样保留、不编数）。默认 `shadow` 是**方向性**的取舍——重述把"确定上界"换成估计，花得比上限慢的 run 会让同线并发在途变多。切 `enforce` 前先看 `subagent_line_projection_tokens` 的分布，判断依据在 `subagent_line_projection_total{outcome=...}`（`raised`/`lowered`/`flat` 的比例就是"猜值偏保守还是偏松"的直接读数）。同一次触发还会**冲刷步骤缓冲**（那条与开关无关，永远生效）：崩溃后补账只能按已落库的步骤行算，逐次冲刷把损失上界从 20 条事件收到当前回合的尾巴 |
| `CHIRON_TEST_POSTGRES_DSN` / `CHIRON_TEST_REDIS_ADDR` | **只给测试用**：置上它们才会跑真实栈的 live 子集 |

## 校验配置本身

```bash
docker compose config --quiet     # compose 语法与变量插值校验
```

## 延伸阅读

- [部署](../production/deploy.md) · [安装](../getting-started/install.md)
- [多实例](../production/multi-instance.md) —— 多副本下必须配对的变量
