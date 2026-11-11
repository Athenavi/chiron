package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// withUserCtx 模拟 TenantMiddleware 注入的身份。
// GetUserID/GetTenantID 读的是这两个 ctx key，**不是** claims ——
// 只挂 claims 会让 handler 一律 401（看起来像实现有问题）。
func withUserCtx(r *http.Request, userID string, tenantID int16) context.Context {
	ctx := context.WithValue(r.Context(), CtxKeyUserID, userID)
	return context.WithValue(ctx, CtxKeyTenantID, tenantID)
}

// ── 个人信息导出 ──
//
// 这个端点把"关于我的全部数据"一次性交出去，风险全在**边界**上：
//   - 多导出一点 = 泄漏凭据或他人数据；
//   - 少导出一点 = 合规交付物不完整，用户以为导全了。
//
// 最关键的一条是「不含认证凭据」：password_hash / totp_secret_enc /
// totp_recovery_hashes / api_keys.key_hash 任何一个出现在响应里，
// 都会把攻击面从"数据库泄露"扩大到"用户邮箱或下载目录泄露"。
// 这条无法靠"读代码觉得没写"来保证 —— 有人加一列 SELECT * 就破了。

// gdprFixture 建一个带完整敏感字段的用户，供导出用例检查边界。
func gdprFixture(t *testing.T) (userID string, rec *httptest.ResponseRecorder, body []byte) {
	t.Helper()
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	ctx := t.Context()

	// 给这个用户填满**所有**敏感字段，这样"导出的 JSON 里不该出现它们"
	// 才是真断言，而不是因为库里本来就是空值。
	if _, err := pool.Exec(ctx, `
		UPDATE users
		   SET password_hash = $2,
		       totp_secret_enc = $3,
		       totp_enabled = true,
		       totp_recovery_hashes = '["deadbeef"]'::jsonb
		 WHERE id = $1`,
		userID, "$2a$10$sentinelpasswordhashvalue", "sentinel-totp-secret-cipher"); err != nil {
		t.Fatalf("填充敏感字段失败: %v", err)
	}

	// 造一条积分流水，确保导出里至少有一类真实数据（而不是空数组蒙混过关）。
	if _, err := pool.Exec(ctx, `
		INSERT INTO credit_transactions (id, user_id, amount, balance, reason, created_at)
		VALUES ($1, $2, 100, 1100, 'gdpr-export-test', now())`,
		"gdpr-tx-"+shortID(userID), userID); err != nil {
		t.Fatalf("插入积分流水失败: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(ctx, `DELETE FROM credit_transactions WHERE user_id = $1`, userID)
	})

	h := mfaTestHandler(t) // 复用：它只需 cfg 与加密密钥
	req := httptest.NewRequest(http.MethodGet, "/v1/me/export", nil)
	rctx := withUserCtx(req, userID, tenantID)
	rec = httptest.NewRecorder()
	h.ExportMyData(rec, req.WithContext(rctx))

	return userID, rec, rec.Body.Bytes()
}

func TestLiveExportMyDataExcludesCredentials(t *testing.T) {
	userID, rec, body := gdprFixture(t)

	if rec.Code != http.StatusOK {
		t.Fatalf("导出失败: status=%d body=%s", rec.Code, rec.Body.String())
	}

	// ① 必须是可解析的 JSON（下载回来打不开等于没导出）。
	var parsed gdprExport
	if err := json.Unmarshal(body, &parsed); err != nil {
		t.Fatalf("导出内容不是合法 JSON: %v", err)
	}
	if parsed.Version == "" || parsed.Account["id"] != userID {
		t.Fatalf("导出缺少版本或账号标识: %+v", parsed.Account)
	}

	// ② 核心断言：响应里**绝不能**出现任何凭据。
	// 断言具体哨兵值而不是字段名，因为字段名可能在其它合法内容里出现；
	// 而这些值是本用例专门写进去的，一旦出现就证明真的泄漏了。
	text := string(body)
	for name, sentinel := range map[string]string{
		"password_hash":        "sentinelpasswordhashvalue",
		"totp_secret_enc":      "sentinel-totp-secret-cipher",
		"totp_recovery_hashes": "deadbeef",
	} {
		if strings.Contains(text, sentinel) {
			t.Fatalf("导出内容泄漏了 %s（要命的是它会被存进用户下载目录）", name)
		}
	}
	// 字段名同样不该出现：连"有个名叫 password_hash 的字段"都不必让用户知道。
	for _, field := range []string{"password_hash", "totp_secret_enc", "totp_recovery_hashes"} {
		if strings.Contains(text, field) {
			t.Fatalf("导出内容出现了敏感字段名 %q", field)
		}
	}

	// ③ 必须有真实数据，否则"没泄漏"可能只是因为什么都没导。
	if len(parsed.Credits) == 0 {
		t.Fatal("导出里没有积分流水：这条路径可能根本没读库")
	}

	// ④ 必须是附件下载（而不是在浏览器里内联渲染 JSON）。
	// 内联渲染会让"分享导出链接"变成一次完整的账号数据泄漏。
	if cd := rec.Header().Get("Content-Disposition"); !strings.Contains(cd, "attachment") {
		t.Fatalf("Content-Disposition 未标记 attachment，得到 %q", cd)
	}
}

// 导出必须按 user_id 过滤：拿到别人的会话/流水就是越权。
func TestLiveExportMyDataScopedToCaller(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, mine := liveSmokeFixture(t, pool)
	_, other := liveSmokeFixture(t, pool)
	ctx := t.Context()

	// 给另一个用户造一条可识别流水。
	sentinelReason := "OTHER-USER-PRIVATE-" + shortID(other)
	if _, err := pool.Exec(ctx, `
		INSERT INTO credit_transactions (id, user_id, amount, balance, reason, created_at)
		VALUES ($1, $2, 999, 9999, $3, now())`,
		"gdpr-other-"+shortID(other), other, sentinelReason); err != nil {
		t.Fatalf("插入他人流水失败: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(ctx, `DELETE FROM credit_transactions WHERE user_id = $1`, other)
	})

	h := mfaTestHandler(t)
	req := httptest.NewRequest(http.MethodGet, "/v1/me/export", nil)
	rec := httptest.NewRecorder()
	h.ExportMyData(rec, req.WithContext(withUserCtx(req, mine, tenantID)))

	if strings.Contains(rec.Body.String(), sentinelReason) {
		t.Fatal("导出里出现了其他用户的数据：数据可携带权不是数据越权")
	}
}

// 未登录一律 401；且身份只来自会话，不能靠 query 参数指定别人。
func TestLiveExportMyDataRequiresIdentity(t *testing.T) {
	withP12Pool(t)
	h := mfaTestHandler(t)

	req := httptest.NewRequest(http.MethodGet, "/v1/me/export?user_id=someone-else", nil)
	rec := httptest.NewRecorder()
	h.ExportMyData(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("未登录应 401，得到 %d body=%s", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "someone-else") {
		t.Fatal("响应回显了 query 里的 user_id")
	}
}
