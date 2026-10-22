# 安全策略

## 报告漏洞

**不要开公开 issue。** 优先走私有渠道：

1. **GitHub Security Advisories**（推荐）：仓库 → Security → *Report a vulnerability*
2. 或邮件：`security@example.com`（维护者替换为真实地址）

报告请包含：受影响的版本或 commit、复现步骤、影响面（越权 / 数据泄露 / RCE / 密钥泄漏…），
以及你认为**被突破的是哪一条信任边界**（认证、租户隔离、执行隔离、内部服务互信）。

## 响应节奏

本项目由维护者在业余时间推进，时限是**尽力而为**：

- 3 个工作日内确认收到；
- 14 天内给出初步判断（是否成立、影响等级、预计修复窗口）；
- 修复发布后，如你愿意，会在 advisory 里署名致谢。

在修复发布前，请不要公开细节（包括在别处顺带提及）。

## 在范围内的

认证 / 授权绕过、跨租户数据访问、SQL 与命令注入、SSRF、沙箱逃逸、任意文件读写、
密钥或凭据泄漏、供应链（依赖与构建脚本）问题。

## 不在范围内的

- 需要先拿到管理员凭据或宿主机 root 才能利用的问题；
- 纯 DoS / 压测；
- 社会工程；
- **部署前提未满足**导致的问题 —— 例如未配置 `TRUSTED_PROXY_CIDRS` 时 IP 限流可被伪造、
  未加 NetworkPolicy 时内部端点可达。这些属于部署方责任，见
  [docs/deployment-multi-instance.md](docs/deployment-multi-instance.md)。

## 部署前提（它们本身是安全边界的一部分）

以下前提不成立时，相应的安全保证也**不成立**：

1. **网关是唯一入口**：引擎与内部端点不应直接暴露在公网。参考
   `deploy/k8s/chiron.yaml` 里的 Service 类型与 NetworkPolicy。
2. **四个密钥必须强随机且全副本同值**：`APP_SECRET`（根密钥，派生 JWT 与内部 token）、
   `JWT_SECRET`、`INTERNAL_TOKEN`、`METRICS_TOKEN`。
3. **执行隔离依赖独立沙箱服务**（`SANDBOX_BACKEND=service`）。进程内沙箱只提高门槛，
   **不是**安全边界；把它写成默认值是为了 fail-closed，不是因为它够安全。
4. **内部数据库端点应使用只读数据库角色**：应用层的只读 SQL 白名单是启发式，
   数据库角色才是硬边界。
5. **多副本部署时 Redis 与 PostgreSQL 是硬依赖**：不可用时系统默认拒绝启动（而非静默降级），
   这是有意的 fail-closed。

## 支持范围

只对默认分支（`main`）提供安全修复；历史版本不单独维护。
