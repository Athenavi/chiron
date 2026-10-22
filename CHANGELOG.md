# 变更记录

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的结构。
版本号采用 `v0.1.YYMMDD.NN`：`YYMMDD` 为发布日期，`NN` 为当日第几次发布。

## [Unreleased]

### 新增

（下一次发布前把变更移到这里）

## [v0.1.261022.01] - 2026-10-22

首个公开准备版本。

### 新增

- **多副本部署**：会话运行锁、SSE 跨实例广播与断线重放、run 归属路由、队列消费组与 PEL 接管、
  实例存活键与在途预留清理
- **多租户**：租户/用户配额、租户级审计与可回放证据链、SSO、计费幂等（`turn_id` 唯一索引）
- **Agent 运行时**：工具授权链、人在环审批（可跨进程答复）、检查点续跑、子 Agent 委派、
  多层记忆、知识库检索、工作流引擎
- **前端**：模型配置页的分组 / 折叠 / 批量启停；管理后台；i18n 三语言

### 变更

- 数据库迁移链收敛为单条权威基线（`0001_authoritative_baseline`）：全新库一次
  `alembic upgrade head` 即达终态
- 连接预算改为经 PgBouncer 收敛；网关与引擎的观测端点统一要求 `METRICS_TOKEN`
- 生产环境判定统一到 `python-engine/app/envmode.py`
  （`CHIRON_ENV` / `APP_ENV` / `PYTHON_ENV` 任一命中即生产）
- 内部 SQL 端点（`/v1/internal/db/*`）统一只读校验（含多语句与 CTE DML 拦截）
- 内部特权端点收敛：gateway Service 改为 `ClusterIP`，并补 NetworkPolicy
- k8s 下 gateway 挂载共享卷（媒体、上传、编辑器工作区与插件配置）
- 优雅终止窗口 30s → 60s 并补 `preStop`；gateway 补反亲和；镜像拉取策略改为 `IfNotPresent`

### 修复

- 沙箱租户白名单的注释与启动日志曾与代码语义相反（空名单 = **全部租户**走服务）
- `SANDBOX_BACKEND` 取值非法时曾静默退回进程内执行（现在拒绝启动）
- CTE 内嵌 DML 的 SQL 闸门漏判（形如 `WITH x AS (DELETE …) SELECT …` 曾被放行）

### 安全

- 漏洞请按 [SECURITY.md](SECURITY.md) 的私有渠道报告；安全修复会在 GitHub advisory 中单独披露
