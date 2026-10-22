# 企业 SSO（SAML / LDAP / SCIM）—— 范围与设计

> **状态：设计与范围，未实现（独立立项）。** 本文只界定"要做什么、怎么做、依赖什么、风险在哪"，
> 不改动任何现有代码。当前唯一的企业身份入口是 **OIDC**。
>
> 与本仓库其它设计文档同一纪律：**把"有机制"与"有保证"分开写**，每条给出验收与边界。

## 1. 现状（已实现）

| 能力 | 位置 | 说明 |
|---|---|---|
| OIDC 登录 | `internal/api/sso_handler.go`、`gateway_router.go`、`ent_oidc_providers` 表 | 授权码流（Go 端拿 code 换 token + 校验 id_token），按租户启用、多 provider |
| 凭据加密 | `internal/settings`（AES-256-GCM，APP_SECRET 派生） | `client_secret_enc` 落库前加密，明文不落库 |
| 公开列表 fail-closed | `sso_handler.go` 的 `isTrustedProxyPeer` | 非可信代理不采信 `Host`，SSO 公开列表返回空 |
| Cookie 安全 | `config` 的 `cookieSecure()` | 按部署协议（`PUBLIC_BASE_URL`）推导 `Secure` |

**没有**：SAML 2.0、LDAP(S) 目录绑定、SCIM 2.0 用户/组供给（provisioning）。

## 2. 为什么这是"缺口"而非"缺陷"

OIDC 覆盖了多数现代 IdP（Okta / Azure AD / Keycloak / Google Workspace 都提供 OIDC）。
但在**大型企业**里，以下场景 OIDC 覆盖不到，属于**新能力**而非修 bug：

- 只提供 SAML 2.0 的旧 IdP（ADFS、部分自研门户）；
- 要求用 **LDAP(S)** 直接绑定内网目录（不经过 IdP）；
- 要求 **SCIM 自动供给**：员工入职自动建号/入组、离职自动停用（HR 系统驱动）。

因此本文按"独立立项"处理：它带来新的协议依赖、新的安全边界与新的运维面。

## 3. 分期方案

### 阶段 1：SAML 2.0 SP（推荐先做）

**目标**：企业 IdP 走 SAML 也能登录，与 OIDC 并存、同一后台页管理。

- **角色**：Chiron 作为 **SP**（Service Provider），IdP 由客户提供元数据（EntityID、SSO URL、x509 证书）。
- **依赖**：Go 侧成熟的 SAML 库（如 `github.com/russellhaering/gosaml2`，纯 Go、无 cgo）。
- **登录流**：SP-initiated（`/v1/auth/saml/{provider}/login` → IdP → ACS `/v1/auth/saml/{provider}/acs`）。
- **校验**（每一条都是硬要求，缺一即安全洞）：
  - 响应签名（`Response` 或 `Assertion`）必须验签，证书**来自该 provider 的配置**（不信任响应里自带证书）；
  - `Audience` = 本 SP 的 EntityID；`Destination` = 本 SP 的 ACS URL；
  - `NotBefore`/`NotOnOrAfter` 有效期校验 + **时钟偏移容忍窗口**；
  - 拒绝重放（`InResponseTo` 与 SP 侧发起时保存的 request id 比对，一次性）；
  - `NameID`/属性 → 用户映射（见 §4）。
- **验收**：单元测试覆盖"篡改断言/过期/错误 Audience/重放"四类拒绝；一个真实 IdP 的手工联调记录。
- **边界**：不做 IdP-initiated 的深链（可选，需额外防 CSRF/重放）；不做 SLO（单点登出，多数 IdP 实现不一）。

### 阶段 2：LDAP(S) 目录绑定

**目标**：用内网目录做认证（不改目录 schema）。

- **两种模式**，需与客户确认：
  - **bind-as-user**：用用户 DN + 密码直接 bind 验证（实现简单，但把用户密码传给应用）；
  - **service-bind + search**：用服务账号 bind 后搜索用户 DN，再以用户 DN bind 验证（推荐）。
- **依赖**：`github.com/go-ldap/ldap/v3`。
- **要处理**：LDAPS 或 StartTLS（**明文 LDAP 必须拒绝**）、连接池/超时、DN 注入（转义 RFC 4515）、
  属性映射（`sAMAccountName`/`uid`/`mail`/`memberOf`）。
- **边界**：LDAP 只做**认证**，不做会话（登录成功后仍签发本系统的 JWT，与 OIDC 一致）。

### 阶段 3：SCIM 2.0 供给（用户/组）

**目标**：IdP/HR 系统通过 SCIM 自动建号、改属性、停用、入组。

- **端点**（对外，走**独立 bearer token**，不复用用户 JWT）：`/scim/v2/Users`、`/scim/v2/Groups`
  （`GET` 列表+过滤、`POST` 创建、`PUT`/`PATCH` 更新、`DELETE` 停用）。
- **映射**：SCIM `userName`/`emails`/`active` → 本系统用户；`Groups` → 租户内角色/组。
- **风险最高**：
  - **多租户归属**：SCIM token 必须**绑定单一租户**，绝不能跨租户建号（否则是越权入口）；
  - 停用语义（`active=false`）：是"禁止登录"还是"删号"？默认禁止登录 + 保留数据（可审计、可恢复）；
  - 与本地注册/邀请码的**优先级**冲突（SCIM 建的号是否允许改密/自助注册）；
  - 幂等与顺序（`externalId` 去重；PATCH 的 `add/replace/remove` 语义）。
- **验收**：与 Okta/Azure AD 的 SCIM 测试工具跑通 Users 全生命周期；跨租户 token 用例必须 403。

## 4. 共用的三个决策点（三个阶段都要先定）

1. **身份归一**：OIDC `sub` / SAML `NameID` / LDAP DN 各自是不透明标识。需要一张
   `identity_links(user_id, provider_type, provider_id, subject)` 表做**稳定绑定**（邮箱可变，不能做主键）。
2. **首次登录建号策略**：自动建号（JIT provisioning）还是必须预先由 SCIM/邀请存在？默认**只允许已存在用户**绑定，
   自动建号需显式开关（避免任意 IdP 用户凭空获得账号）。
3. **租户归属**：每个 provider 绑定一个租户；用户只能进入其 provider 所属租户（与现有 OIDC 一致）。

## 5. 非目标（明确不做）

- 不实现 IdP 侧（不托管用户目录）；
- 不实现 OAuth2 授权服务器（不对外发 token）；
- 不把 SSO 做成"绕过网关直连引擎"的通道（仍走网关认证、限流、审计）。

## 6. 依赖与工作量（粗估）

| 阶段 | 主要工作 | 依赖 | 量级 |
|---|---|---|---|
| 1 SAML SP | SP 元数据/登录/ACS/验签/映射 + 后台页 + 测试 | gosaml2 | 数天 |
| 2 LDAP | bind/search/TLS/属性映射 + 测试 | go-ldap | 数天 |
| 3 SCIM | 标准端点 + token 模型 + 映射 + 生命周期测试 | 无（标准库足够） | 1–2 周 |

## 7. 与现有安全模型的接口

- 所有新入口**必须**：走网关（认证/限流/审计）、写 `audit`、受 `TRUSTED_PROXY_CIDRS` 约束、
  凭据用 `internal/settings` 加密落库；
- 新增的对外端点要进路由探针测试（防止"handler 建了但没接线"——本仓库反复踩过的坑）；
- 每阶段都要有**拒绝用例**（篡改/过期/跨租户/重放），而不只是"happy path 通过"。
