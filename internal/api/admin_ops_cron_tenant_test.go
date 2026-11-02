package api

import (
	"encoding/json"
	"strings"
	"testing"
)

// ── cron_jobs 租户隔离回归（P0）──
//
// 背景：cron_jobs 的列表/创建/更新/删除/触发此前**全部**不带 tenant_id 谓词，
// 于是任意租户的 admin 都能列举、修改、触发、删除别人的定时任务；而 job 的
// tenant_id/user_id 直接决定 python 侧以哪套凭据执行。列表还会把 webhook_token
// （长期有效的触发凭据）明文下发。
//
// 单元测试跑不起真实 PG（仓库约定 DB 用例写 `*_live_test.go`），所以这里断言
// 语句形状：每条租户级语句都必须带 `tenant_id = $n`。语句改错时这些用例会先红。

func TestCronQueriesAreTenantScoped(t *testing.T) {
	cases := []struct {
		name  string
		query string
	}{
		{"list", cronListQuery},
		{"delete", cronDeleteQuery},
		{"load", cronLoadQuery},
		{"reset-token", cronResetTokenQuery},
	}
	for _, c := range cases {
		if !strings.Contains(c.query, "tenant_id = $") {
			t.Errorf("%s 语句缺少 tenant_id 谓词 —— 任意租户都能操作别人的定时任务：\n%s", c.name, c.query)
		}
	}

	// 动态 UPDATE 的谓词是拼进去的，单独校验模板本身不被写成空串。
	if !strings.Contains(cronUpdateTenantPredicate, "tenant_id = $") {
		t.Fatalf("update 谓词模板缺少 tenant_id —— 跨租户改写 task 又回来了：%q", cronUpdateTenantPredicate)
	}
}

// 创建语句必须写入 tenant_id：DDL 是 NOT NULL，漏了列在真实库上直接 500。
func TestCronInsertWritesTenantColumn(t *testing.T) {
	if !strings.Contains(cronInsertQuery, "tenant_id") {
		t.Fatalf("INSERT 未写入 tenant_id（DDL 为 NOT NULL）：\n%s", cronInsertQuery)
	}
	if !strings.Contains(cronInsertQuery, "user_id") {
		t.Errorf("INSERT 未写入 user_id —— 执行时将没有归属身份：\n%s", cronInsertQuery)
	}
}

// 列表响应结构与序列化输出里都不能出现明文 webhook_token。
func TestCronRowDoesNotSerializeWebhookToken(t *testing.T) {
	raw, err := json.Marshal(cronRow{ID: "j1", WebhookSet: true, WebhookHint: "****abcd"})
	if err != nil {
		t.Fatalf("marshal cronRow: %v", err)
	}
	out := string(raw)
	if strings.Contains(out, `"webhook_token"`) {
		t.Errorf("列表响应包含明文 webhook_token：%s", out)
	}
	if !strings.Contains(out, `"webhook_enabled":true`) {
		t.Errorf("列表响应未告知是否已配置 webhook：%s", out)
	}
}

func TestWebhookTokenHintOnlyExposesTail(t *testing.T) {
	const token = "0123456789abcdef0123456789abcdef"
	hint := webhookTokenHint(token)
	if !strings.HasSuffix(hint, token[len(token)-4:]) {
		t.Errorf("hint %q 没有反映末 4 位，用户无法辨认是哪把 token", hint)
	}
	if strings.Contains(hint, token[:8]) {
		t.Errorf("hint %q 泄漏了 token 前缀", hint)
	}
	if len(hint) > 8 {
		t.Errorf("hint %q 过长，暴露的信息过多", hint)
	}
	if webhookTokenHint("") != "" || webhookTokenHint("ab") != "" {
		t.Error("空/过短 token 不应产生 hint")
	}
}

// webhook 触发路径的 token 比较必须是常量时间：原实现用 `storedToken != token`，
// 逐字节提前返回，可被按响应时间差逐位暴力还原（这是唯一凭据，猜中即能触发任务）。
func TestCronWebhookTokenCompareIsConstantTime(t *testing.T) {
	const stored = "0123456789abcdef0123456789abcdef"
	if !cronWebhookTokenEqual(stored, stored) {
		t.Fatal("相同 token 判定为不相等")
	}
	if cronWebhookTokenEqual(stored, stored[:len(stored)-1]+"z") {
		t.Fatal("不同 token 判定为相等")
	}
	// 长度不同也必须安全返回 false（不能 panic，也不能误判相等）。
	if cronWebhookTokenEqual(stored, stored+"x") || cronWebhookTokenEqual(stored, "") {
		t.Fatal("长度不同的 token 判定为相等")
	}
}
