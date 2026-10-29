package api

import (
	"bytes"
	"encoding/csv"
	"errors"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"
)

// TestParseExportRange_DefaultsToLast30Days 验证省略参数时的默认窗口。
//
// 为什么重要：前端第一次接这个接口时不会传 from/to（用户还没选范围），
// 默认值必须是一个有意义且不会全表扫描的窗口。
func TestParseExportRange_DefaultsToLast30Days(t *testing.T) {
	now := time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)

	from, to, err := parseExportRange("", "", now)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !to.Equal(now) {
		t.Errorf("to should default to now, got %v", to)
	}
	// AddDate 而不是 Add(-30*24h)：夏令时切换时后者会偏一小时。
	want := now.AddDate(0, 0, -30)
	if !from.Equal(want) {
		t.Errorf("from should be 30 days before to: want %v, got %v", want, from)
	}
}

// TestParseExportRange_RejectsBadInput 验证非法参数被拒绝而不是静默回落。
//
// 静默回落的坏处：用户以为导的是某个范围，实际拿到的是另一个范围，
// 对账时看不出来，这种错误比直接报错昂贵得多。
func TestParseExportRange_RejectsBadInput(t *testing.T) {
	now := time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)

	cases := []struct {
		name    string
		from    string
		to      string
		wantErr string
	}{
		{"from 非 RFC3339", "2026-10-01", "", "from must be RFC3339"},
		{"to 非 RFC3339", "", "not-a-time", "to must be RFC3339"},
		{"to 早于 from", "2026-10-29T00:00:00Z", "2026-10-01T00:00:00Z", "to must be after from"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, _, err := parseExportRange(tc.from, tc.to, now)
			if err == nil {
				t.Fatal("expected error, got nil")
			}
			if err.Error() != tc.wantErr {
				t.Errorf("want %q, got %q", tc.wantErr, err.Error())
			}
		})
	}
}

// TestParseExportRange_RejectsOverlyWideSpan 验证跨度上限。
//
// 这是唯一的"防全表扫描"闸门：没有它，`from=1970-01-01` 就是一个免费的
// 数据库压力测试入口 —— 而这个端点是同步的，会一直占着连接。
func TestParseExportRange_RejectsOverlyWideSpan(t *testing.T) {
	now := time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)

	// 刚好在上限内：应通过。
	from := now.AddDate(0, 0, -exportMaxSpanDays)
	if _, _, err := parseExportRange(from.Format(time.RFC3339), now.Format(time.RFC3339), now); err != nil {
		t.Fatalf("span at the limit should be allowed, got %v", err)
	}

	// 超过一天：应拒绝。
	from = now.AddDate(0, 0, -exportMaxSpanDays-1)
	_, _, err := parseExportRange(from.Format(time.RFC3339), now.Format(time.RFC3339), now)
	if !errors.Is(err, errExportRangeTooWide) {
		t.Fatalf("expected errExportRangeTooWide, got %v", err)
	}
}

// TestCSVSafeText_NeutralisesFormulaPrefixes 验证 CSV 公式注入防护。
//
// Excel 会把以 = + - @ 开头的单元格当公式执行。当前 reason 是内部枚举，
// 风险低，但这条防护是给"将来接入用户可控文本"留的，成本几乎为零。
func TestCSVSafeText_NeutralisesFormulaPrefixes(t *testing.T) {
	safe := map[string]string{
		"":                 "",
		"llm_call":         "llm_call",
		"recharge":         "recharge",
		"=cmd|calc":        "'=cmd|calc",
		"+1+1":             "'+1+1",
		"-42":              "'-42",
		"@SUM(A1)":         "'@SUM(A1)",
		"正常的中文事由":         "正常的中文事由",
	}
	for in, want := range safe {
		if got := csvSafeText(in); got != want {
			t.Errorf("csvSafeText(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestFormatTransactionRecord_KeepsAmountsNumeric 验证金额列**不**被加引号前缀。
//
// 这是 csvSafeText 只作用于文本列的原因：金额天然可能为负，加前缀后
// Excel 里变成文本，用户再也算不了和 —— 修复一个几乎不存在的风险，
// 换来一个必然发生的功能缺陷。
func TestFormatTransactionRecord_KeepsAmountsNumeric(t *testing.T) {
	row := exportRow{
		CreatedAt: time.Date(2026, 10, 29, 1, 2, 3, 0, time.UTC),
		Reason:    "llm_call",
		Amount:    -42,
		Balance:   958,
		TurnID:    "turn-abc",
	}

	got := formatTransactionRecord(row)
	want := []string{"2026-10-29T01:02:03Z", "llm_call", "-42", "958", "turn-abc"}

	if !slices.Equal(got, want) {
		t.Fatalf("unexpected record:\n want %#v\n  got %#v", want, got)
	}
}

// TestFormatTransactionRecord_TimeIsUTCAndTurnIDMayBeEmpty 验证时区与空轮次。
//
// created_at 是无时区的 timestamp（roadmap §10.2 / D 组 L4-3）。统一按 UTC
// 输出并在表头注明，否则跨时区对账会差几个小时而用户毫无线索。
func TestFormatTransactionRecord_TimeIsUTCAndTurnIDMayBeEmpty(t *testing.T) {
	shanghai := time.FixedZone("CST", 8*3600)
	row := exportRow{
		CreatedAt: time.Date(2026, 10, 29, 9, 2, 3, 0, shanghai), // = 01:02:03Z
		Reason:    "recharge",
		Amount:    500,
		Balance:   1500,
		TurnID:    "", // 充值类记录没有关联轮次
	}

	got := formatTransactionRecord(row)
	if got[0] != "2026-10-29T01:02:03Z" {
		t.Errorf("time must be rendered in UTC, got %q", got[0])
	}
	if got[4] != "" {
		t.Errorf("empty turn_id must stay empty (not \"-\"), got %q", got[4])
	}
}

// TestWriteCSV_EmitsBOMHeaderAndRows 验证导出文件的整体形态。
//
// 三件事一起验，因为它们任一处做错都会让文件"看起来正常但打不开"：
// BOM 决定 Excel 是否认 UTF-8、CRLF 决定旧版 Excel 是否分行、
// 表头决定用户能否看懂列。
func TestWriteCSV_EmitsBOMHeaderAndRows(t *testing.T) {
	rows := []exportRow{
		{
			CreatedAt: time.Date(2026, 10, 28, 1, 0, 0, 0, time.UTC),
			Reason:    "llm_call",
			Amount:    -10,
			Balance:   990,
			TurnID:    "t1",
		},
		{
			CreatedAt: time.Date(2026, 10, 29, 2, 0, 0, 0, time.UTC),
			Reason:    "recharge",
			Amount:    500,
			Balance:   1490,
			TurnID:    "",
		},
	}

	var buf bytes.Buffer
	if err := writeCSV(&buf, slices.Values(rows)); err != nil {
		t.Fatalf("writeCSV failed: %v", err)
	}

	raw := buf.Bytes()

	// ① BOM 必须在最前面 —— Excel 只看文件开头。
	if !bytes.HasPrefix(raw, utf8BOM) {
		t.Fatalf("output must start with UTF-8 BOM, got % x", raw[:min(8, len(raw))])
	}

	// ② 去掉 BOM 后应是一个语法完整的 CSV。
	rd := csv.NewReader(bytes.NewReader(raw[len(utf8BOM):]))
	records, err := rd.ReadAll()
	if err != nil {
		t.Fatalf("output is not valid CSV: %v", err)
	}

	if len(records) != len(rows)+1 {
		t.Fatalf("expected %d records (header + rows), got %d", len(rows)+1, len(records))
	}
	if !slices.Equal(records[0], exportCSVHeader) {
		t.Errorf("header mismatch:\n want %#v\n  got %#v", exportCSVHeader, records[0])
	}
	if records[1][2] != "-10" || records[2][3] != "1490" {
		t.Errorf("row content mismatch: %#v", records[1:])
	}

	// ③ CRLF 行结束符：RFC 4180 的约定，老版 Excel 只认它。
	// 断言表头最后一列（中文、不含逗号因此不带引号）后面跟的是 \r\n。
	if !bytes.Contains(raw, []byte("关联轮次\r\n")) {
		t.Errorf("rows should be terminated with CRLF, got tail %q",
			string(raw[max(0, len(raw)-40):]))
	}
}

// TestWriteCSV_EmptyRangeStillHasHeader 验证"没有流水"时也产出可打开的文件。
//
// 返回一个只有表头的 CSV 而不是空文件：用户在 Excel 里能看出"这个范围确实
// 没有记录"，而空文件看起来像导出失败。
func TestWriteCSV_EmptyRangeStillHasHeader(t *testing.T) {
	var buf bytes.Buffer
	if err := writeCSV(&buf, slices.Values([]exportRow{})); err != nil {
		t.Fatalf("writeCSV failed: %v", err)
	}

	rd := csv.NewReader(bytes.NewReader(buf.Bytes()[len(utf8BOM):]))
	records, err := rd.ReadAll()
	if err != nil {
		t.Fatalf("output is not valid CSV: %v", err)
	}
	if len(records) != 1 {
		t.Fatalf("expected header only, got %d records", len(records))
	}
}

// TestCheckExportRowLimit_RejectsOnlyAboveLimit 验证行数上限的边界。
//
// 边界取 `>` 而不是 `>=`：range 内恰好 50000 行时，LIMIT 50000 拿到的就是
// 全部数据，此时报错属于误伤 —— 用户被拦下却找不到任何可以缩小的东西。
func TestCheckExportRowLimit_RejectsOnlyAboveLimit(t *testing.T) {
	cases := []struct {
		total     int
		wantError bool
	}{
		{0, false},
		{1, false},
		{exportMaxRows - 1, false},
		{exportMaxRows, false},     // 恰好等于上限：数据完整，允许
		{exportMaxRows + 1, true},  // 多一行就开始丢数据，必须拦
		{exportMaxRows * 4, true},
	}

	for _, tc := range cases {
		err := checkExportRowLimit(tc.total)
		if tc.wantError && err == nil {
			t.Errorf("total=%d: expected rejection, got nil", tc.total)
		}
		if !tc.wantError && err != nil {
			t.Errorf("total=%d: expected pass, got %v", tc.total, err)
		}
	}
}

// TestCheckExportRowLimit_ErrorCarriesCount 验证错误文案带上实际行数。
//
// 只说"太多了"用户不知道该缩小到多少；带上行数至少让他能判断是差一点
// 还是差一个数量级。
func TestCheckExportRowLimit_ErrorCarriesCount(t *testing.T) {
	err := checkExportRowLimit(exportMaxRows + 1234)
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(err.Error(), strconv.Itoa(exportMaxRows+1234)) {
		t.Errorf("error should mention the actual count, got %q", err.Error())
	}
}

// TestExportQueries_AreScopedToOneUserAndExcludeFreeChat 验证两条 SQL 的隔离口径。
//
// 这是「用户隔离」在无数据库环境下的可测形态：导出只按 `user_id = $1` 过滤，
// 而 $1 在 handler 里只可能来自鉴权 claims（不接受查询参数覆盖）；
// 同时两条查询都必须排除 free_chat，否则"页面上 50 条"与"导出的内容"
// 会对不上 —— 而两边其实都没错。
//
// 断言字符串而不是跑真库：仓库没有 live DB 测试环境（见 roadmap §8.1），
// 但有人改这两条 SQL 时，这条测试会立刻失败。
func TestExportQueries_AreScopedToOneUserAndExcludeFreeChat(t *testing.T) {
	for name, query := range map[string]string{
		"count": exportCountQuery,
		"rows":  exportRowsQuery,
	} {
		t.Run(name, func(t *testing.T) {
			if !strings.Contains(query, "user_id = $1") {
				t.Errorf("query must be scoped to $1 (user_id from claims):\n%s", query)
			}
			if !strings.Contains(query, "reason <> 'free_chat'") {
				t.Errorf("query must exclude free_chat like GetHistory:\n%s", query)
			}
			if !strings.Contains(query, "created_at >= $2") ||
				!strings.Contains(query, "created_at <= $3") {
				t.Errorf("query must bound the range by $2/$3:\n%s", query)
			}
			// 时间区间必须是闭区间两端都带，且是参数化的 —— 不接受任何
			// 由调用方拼进来的字面量。
			if strings.Contains(query, "user_id = '") {
				t.Errorf("user_id must never be inlined as a literal:\n%s", query)
			}
		})
	}
}

// TestExportRowsQuery_IsOrderedAndLimited 验证导出顺序与 LIMIT。
//
// 正序（ASC）而不是页面用的倒序：对账是按时间往下读的，最早的记录在前。
// 末尾再加 id 作为决胜键，保证同一秒的多条记录顺序稳定（否则两次导出
// 的行序可能不同，用户逐行比对时会困扰）。
func TestExportRowsQuery_IsOrderedAndLimited(t *testing.T) {
	if !strings.Contains(exportRowsQuery, "ORDER BY created_at ASC") {
		t.Errorf("export must be in ascending time order:\n%s", exportRowsQuery)
	}
	if !strings.Contains(exportRowsQuery, "id ASC") {
		t.Errorf("export needs a stable tie-breaker:\n%s", exportRowsQuery)
	}
	if !strings.Contains(exportRowsQuery, "LIMIT $4") {
		t.Errorf("export must carry a parameterised LIMIT:\n%s", exportRowsQuery)
	}
	// count 查询不该有 LIMIT：它就是要数全部，否则上限判定失去意义。
	if strings.Contains(exportCountQuery, "LIMIT") {
		t.Errorf("count query must not be limited:\n%s", exportCountQuery)
	}
}
//
// 两个约束：必须是 attachment（否则浏览器可能直接渲染 CSV），
// 文件名必须全是 ASCII 且不含冒号（Windows 文件系统不允许冒号）。
func TestExportDisposition_IsAttachmentWithASCIIName(t *testing.T) {
	from := time.Date(2026, 9, 29, 0, 0, 0, 0, time.UTC)
	to := time.Date(2026, 10, 29, 12, 30, 0, 0, time.UTC)

	got := exportDisposition(from, to)

	if !strings.HasPrefix(got, "attachment;") {
		t.Errorf("must be an attachment, got %q", got)
	}
	if !strings.Contains(got, "20260929T0000_20261029T1230") {
		t.Errorf("filename should carry the range, got %q", got)
	}
	if strings.Contains(got, ":") {
		t.Errorf("filename must not contain a colon, got %q", got)
	}
	// 只保留 ASCII：非 ASCII 文件名在某些浏览器/代理上会被错误编码。
	for _, r := range got {
		if r > 127 {
			t.Fatalf("filename must be ASCII-only, found %q in %q", r, got)
		}
	}
}
