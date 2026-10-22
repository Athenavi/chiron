# 发布说明 · `V0.1.261022.01`

> 2026-10-22 第 1 次发布。距上一个 tag `V0.1.261019.01` 共 **28 个提交**（M19 → M30）。
> 本页只写**升级时需要知道的事**：新增迁移、默认值变化、以及上一版没有的修复。
> 逐批的取舍与护栏在 [开发路线图](development-roadmap.md) §1，架构背景在 [架构说明](architecture.md)。

## 一、新增迁移（4 个）

按顺序：`0026_run_usage_settlements` → `0027_drop_python_settings` → `0028_key_weights` → `0029_model_prices`。

```bash
alembic upgrade head          # 单 head：0029_model_prices
```

⚠ **从空库建整套 schema 需要本版才修好的一个前置**（见下节"干净库跑不到 head"）——旧版本在空库上**跑不完**。

## 二、默认值变化（**升级前请逐条确认**）

这四个开关在 `.env.example` 里已是**生效**状态，新部署复制模板即生效；**已有部署要显式检查自己的 `.env`**。

| 开关 | 本版值 | 生效后会发生什么 |
|---|---|---|
| `SUBAGENT_LINE_PROJECTION` | `enforce` | 在途预留按观测速率**覆盖式重述** ⇒ 同线并发在途的估计**变松**（方向是换的，不是单向收紧） |
| `RUN_USAGE_RECONCILE_MODE` | `enforce` | 按-run 对账**真的扣用户余额**（对账崩溃/自动接管段，写 `billing_records` 补偿明细） |
| `SESSION_LINE_GATE` | `enforce` | 主 run 到会话线上限会被**真的拦下**（硬拦 + 挂提额待办） |
| `MODEL_PRICE_SYNC` | `on` | 每 30min 拉两个公开价格源并交叉验证。⚠ 实测**分歧率 49%**（部分模型两源系统性差 2 倍）⇒ 实际写入的只有两源确认过的那些行；它**不参与计费** |

代码里的**默认值仍是保守值**（前三个 `shadow`、最后一个关）——切的是部署配置。要回到保守状态，把这几行改回 `shadow` / 注释掉即可。

> **2026-10-22 复核**：这四个值是**有意保持**的（用户拍板 —— 模板即新部署的起点，接受"装上去就收紧"）。若你的部署不想一开始就扣余额 / 拦主 run，把这四行改回 `shadow` 或注释掉即可：代码默认本来就是保守的（三个 `shadow` + 一个关），模板只是**覆盖**了它们。

## 三、本版修掉的问题（升级才有意义的部分）

**① 干净的库跑不到 head**（严重，本版新修）
alembic 自建 `alembic_version` 时用默认 `varchar(32)`，而本仓的 revision id 有 **39 字符** ⇒ 跑到 `0017` 会 `StringDataRightTruncation`，**整个事务回滚**（库里连 `alembic_version` 都不留下，症状是"迁移静默失败"）。修法是在 `migrations/env.py` 里预建一张更宽的版本表（幂等；已有表则只加宽）。

**② credits 配额池"能配不检查"**
`ent_quota_pools.resource_type` 允许 `credits`、后台也有"积分"选项，但执行侧只查 `token` ⇒ 配了也不生效。现在按 `credit_transactions` 的扣减量只读判定 + 到限硬拦。

**③ 引擎 `cost` 轴从不触发**
`TaskBudget.exceeded()` 支持 cost 判定但调用方从不传 `used`。现在单价由网关下发（`llm_config.cost_per_1k_*`），引擎按全程 token 换算并参与判定；跨语言字段名由 `scripts/check_tool_policy_parity.py` 的第八族钉住。

**④ 后台「系统设置」的 python 段整段无效**
那 8 个字段全库零读取点（各有真实入口：模型服务 / 引擎 env / `agent.max_turns`）⇒ 整段摘除 + 迁移 `0027` 删旧键。

**⑤ 主 run 不在线账之内**
线账只统计 `subagent_runs` ⇒ 只有主 run 的会话（最常见形态）不受线级上限约束。现在主 run 也过闸门（会话线，账本来自 `turns`）。

**⑥ KeyRing 只能等权轮转**
新增每 key 权重（smooth WRR，默认 1 = 等权 = 旧行为）；`weight = 0` 表示不参与选择。

## 四、本版的验证口径（发布前现场重跑）

| 项 | 结果 |
|---|---|
| Go `build` / `vet` / `test` | 全绿（19 包 ok） |
| 引擎 `ruff` / `mypy` | 0 / 0（278 文件） |
| 引擎全量 `pytest -m "not integration"` | **2838 passed / 43 deselected** |
| 前端 `vitest` / `vue-tsc` | **620 passed（80 文件）** / 0 |
| 七个守卫 | 全 exit 0 |
| 迁移 | 单 head `0029_model_prices` |
| 真库 `pytest -m integration` | **40 passed / 3 skipped / 0 failed** |

## 五、已知限制

- **`integration` 有 3 条 skip**：1 条原有，另 2 条（`test_unified_db_*`）走的是**网关的内部 API**（不是直连库）⇒ 需要**运行中的 Go 网关**才会真跑。
- **本版的分析类文档不入库**（6 份，含缺陷清单本体）：发布包不含它们，逐批取舍以 [开发路线图](development-roadmap.md) §1 为准。
- **存量摘要向量回填**：模块已就绪（`app/memory/backfill.py`，**默认 dry-run**、幂等），已在真库上验证过逻辑；真 embedding 需要 API key。
- **`MODEL_PRICE_SYNC` 的跨源模型名对齐是启发式**：对齐不上就判"单源"、不写入（宁可缺数据）。

## 六、升级步骤

1. 备份数据库；
2. `alembic upgrade head`（会依次跑 `0026`–`0029`；本版修好的 `env.py` 会先保证版本表够宽）；
3. 对照上面第二节**逐条确认那四个开关**在你部署里的取值；
4. 拉起新镜像；`GET /health` 与 `/v1/internal/*` 的自检通过即可。
