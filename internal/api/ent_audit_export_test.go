package api

import (
	"bytes"
	"encoding/csv"
	"net/url"
	"slices"
	"strings"
	"testing"
	"time"
)

// mapToValues 把 map 转成 url.Values（测试构造查询参数用）。
func mapToValues(m map[string]string) url.Values {
	v := url.Values{}
	for k, val := range m {
		v.Set(k, val)
	}
	return v
}

// ent_audit_export_test.go —— 审计导出与时间范围收敛的纯函数契约（M3）。
//
// 只测不依赖数据库的部分：表头与行列数一致、用户可控文本走公式注入防护、
// 时间参数解析（本地时区 / `to` 含当天 / 跨度收敛）、文件名。
// 数据库路径（租户隔离、count 先行）由 handler 的 SQL 常量和端到端复测覆盖。

// TestAuditExportHeader_MatchesRowWidth 验证表头与行宽一致。
//
// 列数错位是 CSV 导出最隐蔽的错误：文件能打开，但「IP」列显示的是「详情」，
// 用户到对账时才发现。表头与 formatAuditExportRow 必须同步改。
func TestAuditExportHeader_MatchesRowWidth(t *testing.T) {
	uid := "u-1"
	resID := "r-1"
	ip := "127.0.0.1"
	row := formatAuditExportRow(auditLogRow{
		ID:           "a-1",
		TenantID:     "1",
		UserID:       &uid,
		Action:       "POST /v1/chat/submit",
		ResourceType: "/v1/chat/submit",
		ResourceID:   &resID,
		Details:      map[string]any{"status": 200},
		IPAddress:    &ip,
		CreatedAt:    time.Date(2026, 11, 8, 10, 0, 0, 0, time.UTC),
	})
	if len(row) != len(auditExportHeader) {
		t.Fatalf("row has %d columns, header has %d", len(row), len(auditExportHeader))
	}
}

// TestAuditExportRow_NeutralisesFormulaInjection 验证用户可控文本被转义。
//
// action / resource_id / details 都可能带上用户可控内容（资源名、错误原文）。
// 以 `=`/`+`/`-`/`@` 开头的内容在 Excel 里会被当公式执行 ——
// 这是真实的攻击面，不是理论风险。
func TestAuditExportRow_NeutralisesFormulaInjection(t *testing.T) {
	uid := "=cmd|calc"
	resID := "@SUM(A1)"
	ip := "-1"
	row := formatAuditExportRow(auditLogRow{
		ID:           "a-1",
		TenantID:     "1",
		UserID:       &uid,
		Action:       "+1+1",
		ResourceType: "chat",
		ResourceID:   &resID,
		Details:      "=HYPERLINK(\"http://evil\")",
		IPAddress:    &ip,
		CreatedAt:    time.Date(2026, 11, 8, 10, 0, 0, 0, time.UTC),
	})

	// 列序见 auditExportHeader：0 时间 1 租户 2 用户 3 动作 4 资源类型 5 资源ID 6 IP 7 详情
	for _, idx := range []int{2, 3, 5, 6} {
		if !strings.HasPrefix(row[idx], "'") {
			t.Errorf("column %d (%q) must be prefixed to neutralise formula injection", idx, row[idx])
		}
	}
	// details 走 JSON 序列化，天然以 `"` 包裹字符串标量 —— 这正是它不需要
	// `'` 前缀的原因（`"=cmd"` 在 Excel 里是文本，不是公式）。断言改成
	// 「首字符不在危险集合内」，既守住安全边界，又不要求实现做无意义的二次转义。
	if dangerousPrefix(row[7]) {
		t.Errorf("details must not start with a formula prefix, got %q", row[7])
	}
	// 时间与租户不是用户可控文本，不应被加前缀。
	if row[0] != "2026-11-08 10:00:00" {
		t.Errorf("created_at must keep its wall-clock format, got %q", row[0])
	}
	if row[1] != "1" {
		t.Errorf("tenant_id must stay as-is, got %q", row[1])
	}
}

// dangerousPrefix 判断单元格内容是否会被 Excel 当公式执行。
//
// 与 csvSafeText 的前缀集合保持一致，单独抽出来是为了让测试能表达
// 「details 经 JSON 序列化后已安全」这条约束，而不是生硬地要求 `'` 前缀。
func dangerousPrefix(s string) bool {
	if s == "" {
		return false
	}
	switch s[0] {
	case '=', '+', '-', '@', '\t', '\r':
		return true
	}
	return false
}

// TestAuditExportRow_HandlesNullsAndDetails 验证可空列与详情序列化。
//
// user_id / resource_id / ip_address 在库里都可空（系统事件没有用户）。
// 导出的空单元格必须留空而不是 "<nil>" 或 "null" —— 后者会让对账的人
// 以为存在一个叫 "nil" 的用户。
func TestAuditExportRow_HandlesNullsAndDetails(t *testing.T) {
	row := formatAuditExportRow(auditLogRow{
		ID:           "a-1",
		TenantID:     "1",
		Action:       "session.create",
		ResourceType: "session",
		Details:      nil,
		CreatedAt:    time.Date(2026, 11, 8, 10, 0, 0, 0, time.UTC),
	})
	for _, idx := range []int{2, 5, 6, 7} {
		if row[idx] != "" {
			t.Errorf("null column %d must stay empty, got %q", idx, row[idx])
		}
	}

	// 有详情时必须序列化成可读 JSON，而不是 Go 的 map 字面量。
	withDetails := formatAuditExportRow(auditLogRow{
		TenantID:     "1",
		Action:       "x",
		ResourceType: "y",
		Details:      map[string]any{"status": float64(200)},
		CreatedAt:    time.Date(2026, 11, 8, 10, 0, 0, 0, time.UTC),
	})
	if !strings.Contains(withDetails[7], `"status"`) || !strings.Contains(withDetails[7], "200") {
		t.Errorf("details must be JSON, got %q", withDetails[7])
	}
}

// TestAuditExportDisposition_IsAttachmentWithASCIIName 验证下载文件名。
//
// 纯 ASCII 且不含冒号：中文文件名在部分浏览器会按错误编码落盘，
// 冒号在 Windows 文件系统上非法。
func TestAuditExportDisposition_IsAttachmentWithASCIIName(t *testing.T) {
	got := auditExportDisposition(time.Date(2026, 11, 8, 10, 30, 0, 0, time.UTC))
	if !strings.HasPrefix(got, `attachment; filename="`) {
		t.Fatalf("must be an attachment: %q", got)
	}
	if !strings.Contains(got, "audit_20261108T1030") {
		t.Errorf("filename must carry the UTC timestamp: %q", got)
	}
	if strings.Contains(got, ":") {
		t.Errorf("filename must not contain a colon: %q", got)
	}
}

// TestParseAuditRange_ExplicitToIncludesWholeDay 验证 `to` 为纯日期时含当天。
//
// 这是"审计页明明有记录却查不到"的历史根因：前端默认 `to=今天`，
// 若按今天 00:00 解释，今天发生的记录会被整体排除。
func TestParseAuditRange_ExplicitToIncludesWholeDay(t *testing.T) {
	now := time.Date(2026, 11, 8, 12, 0, 0, 0, time.Local)
	q := mapToValues(map[string]string{"from": "2026-11-01", "to": "2026-11-08"})

	from, to, err := parseAuditRange(q, now, auditMaxRange)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	wantTo := time.Date(2026, 11, 9, 0, 0, 0, 0, time.Local)
	if !to.Equal(wantTo) {
		t.Errorf("to = %s, want %s（纯日期应含当天）", to, wantTo)
	}
	if !from.Before(to) {
		t.Errorf("from must be before to")
	}
}

// TestParseAuditRange_ConvergesOverWideSpan 验证超跨度收敛而不是报错。
//
// 用户选的是一段真实区间；直接拒绝会让"我就想导这一年"变成无从下手。
// 收敛后至少能拿到最近一段，且上界保持不变。
func TestParseAuditRange_ConvergesOverWideSpan(t *testing.T) {
	now := time.Date(2026, 11, 8, 12, 0, 0, 0, time.UTC)
	q := mapToValues(map[string]string{"from": "2020-01-01", "to": "2026-11-08"})

	from, to, err := parseAuditRange(q, now, auditMaxRange)
	if err != nil {
		t.Fatalf("over-wide span must not error, got %v", err)
	}
	if to.Sub(from) > auditMaxRange {
		t.Errorf("span %s exceeds limit %s", to.Sub(from), auditMaxRange)
	}
	if !to.Equal(time.Date(2026, 11, 9, 0, 0, 0, 0, time.Local)) {
		t.Errorf("upper bound must be preserved, got %s", to)
	}
}

// TestParseAuditRange_RejectsInvalidInput 验证非法输入被拒绝。
func TestParseAuditRange_RejectsInvalidInput(t *testing.T) {
	now := time.Now()
	if _, _, err := parseAuditRange(mapToValues(map[string]string{"from": "not-a-date"}), now, auditMaxRange); err == nil {
		t.Error("invalid from must error")
	}
	if _, _, err := parseAuditRange(mapToValues(map[string]string{"to": "not-a-date"}), now, auditMaxRange); err == nil {
		t.Error("invalid to must error")
	}
	// from >= to 必须报错，而不是静默产出一个空窗口。
	q := mapToValues(map[string]string{"from": "2026-11-08", "to": "2026-11-01"})
	if _, _, err := parseAuditRange(q, now, auditMaxRange); err == nil {
		t.Error("from >= to must error")
	}
}

// TestAuditExportSpanIsWiderThanQuery 验证导出跨度上限确实比查询宽。
//
// 导出是"拿走对账"，沿用查询的 7 天会让用户以为内容被截断；
// 这条断言防止将来有人把两个常量合并成一个。
func TestAuditExportSpanIsWiderThanQuery(t *testing.T) {
	if auditExportMaxSpan <= auditMaxRange {
		t.Fatalf("export span (%s) must exceed query span (%s)", auditExportMaxSpan, auditMaxRange)
	}
	if auditExportMaxRows <= 0 {
		t.Fatalf("export row limit must be positive, got %d", auditExportMaxRows)
	}
}

// TestWriteCSVRows_AuditHeaderRoundTrip 验证审计头 + 行的输出能被标准 CSV 解析。
//
// 与发票导出共用 writeCSVRows，这里单独再钉一次审计表头：
// 若有人把表头改成含逗号/引号的字符串，BOM 段之后仍是合法 CSV 是硬约束。
func TestWriteCSVRows_AuditHeaderRoundTrip(t *testing.T) {
	rows := [][]string{
		formatAuditExportRow(auditLogRow{
			ID:           "a-1",
			TenantID:     "1",
			Action:       "POST /v1/chat/submit",
			ResourceType: "/v1/chat/submit",
			Details:      map[string]any{"path": "/v1/chat/submit"},
			CreatedAt:    time.Date(2026, 11, 8, 10, 0, 0, 0, time.UTC),
		}),
	}

	var buf bytes.Buffer
	seq := func(yield func([]string) bool) {
		for _, r := range rows {
			if !yield(r) {
				return
			}
		}
	}
	if err := writeCSVRows(&buf, auditExportHeader, seq); err != nil {
		t.Fatalf("writeCSVRows failed: %v", err)
	}

	out := buf.Bytes()
	if !bytes.HasPrefix(out, utf8BOM) {
		t.Fatalf("output must start with UTF-8 BOM")
	}
	r := csv.NewReader(bytes.NewReader(out[len(utf8BOM):]))
	parsed, err := r.ReadAll()
	if err != nil {
		t.Fatalf("output is not valid CSV: %v", err)
	}
	want := [][]string{auditExportHeader, rows[0]}
	if !slices.EqualFunc(parsed, want, slices.Equal[[]string]) {
		t.Fatalf("unexpected csv:\n want %#v\n  got %#v", want, parsed)
	}
}
