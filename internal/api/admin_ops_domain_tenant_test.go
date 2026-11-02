package api

import (
	"strings"
	"testing"
)

// ── domains 租户隔离回归（P0）──
//
// 背景：`domains` 决定**登录入口** —— sso_handler.go 的 resolveTenantByHost 用
// `lower(domain) = Host AND verified` 反查 tenant_id，再决定登录页展示哪个租户的 IdP。
// 此前列表/更新/删除/验证/续证**全部**不带 tenant_id 谓词：
//
//   - 列表：任意租户 admin 能看到全平台域名（别家租户的入口清单）；
//   - 更新：改掉别人的域名 → 该域名 `verified=false` → 对方 SSO 入口失效；
//     反过来把域名改成自己的，等于劫持对方的登录流量；
//   - 删除：直接删掉别人的域名；
//   - 验证/续证：替别人刷新 verified / ssl_status。
//
// 这些端点在 routes_admin.go 里挂的是租户级 adminWriteMW（不是平台 owner），所以
// 归属谓词是唯一的隔离。单元测试跑不起真实 PG（仓库约定 DB 用例写 `*_live_test.go`），
// 因此这里断言语句形状：任何一条漏掉谓词，用例立刻变红。

func TestDomainQueriesAreTenantScoped(t *testing.T) {
	cases := []struct {
		name  string
		query string
	}{
		{"count", domainCountQuery},
		{"list", domainListQuery},
		{"update", domainUpdateQuery},
		{"delete", domainDeleteQuery},
		{"load", domainLoadQuery},
		{"set-verified", domainSetVerifiedQuery},
		{"load-verified", domainLoadVerifiedQuery},
		{"set-ssl", domainSetSSLQuery},
	}
	for _, c := range cases {
		if !strings.Contains(c.query, "tenant_id = $") {
			t.Errorf("%s 语句缺少 tenant_id 谓词 —— 租户 admin 可跨租户读写域名（等于操作别人的登录入口）：\n%s",
				c.name, c.query)
		}
	}
}

// 计数与分页必须用同一个谓词：否则 total 是全平台数量，前端显示"共 N 条"
// 而列表里只有本租户的几条，翻页也翻不出内容。
func TestDomainCountMatchesListPredicate(t *testing.T) {
	if !strings.Contains(domainCountQuery, "tenant_id = $1") {
		t.Fatalf("计数语句未按租户过滤：%s", domainCountQuery)
	}
	if !strings.Contains(domainListQuery, "tenant_id = $1") {
		t.Fatalf("列表语句未按租户过滤：%s", domainListQuery)
	}
}

// 改写域名会重置 verified/ssl_status，因此 UPDATE 的归属谓词不能省 —— 省掉就等于
// "替任意租户下线入口"。同时 id 与 tenant_id 都必须是参数（不能拼进 SQL）。
func TestDomainUpdateScopesByTenantAndId(t *testing.T) {
	if !strings.Contains(domainUpdateQuery, "WHERE id = $2 AND tenant_id = $3") {
		t.Fatalf("域名更新未同时以 id 与 tenant_id 定位：%s", domainUpdateQuery)
	}
	if strings.Contains(domainUpdateQuery, "WHERE id = $1") {
		t.Fatalf("域名更新仍以 id 作为第一个参数定位，容易漏掉归属：%s", domainUpdateQuery)
	}
}
