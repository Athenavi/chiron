package api

import (
	"bytes"
	"encoding/csv"
	"slices"
	"strings"
	"testing"
	"time"
)

// invoice_export_test.go —— 发票 CSV 导出的纯函数契约。
//
// 这里只测不依赖数据库的部分：表头与行列数一致、用户可控文本走公式注入防护、
// 金额列保持数值、分转元不丢精度。数据库路径（count 先行、租户隔离）
// 由 handler 的分支测试与 SQL 常量断言覆盖 —— 与 billing_export_test.go 同款分层。

// TestInvoiceExportHeader_MatchesRowWidth 验证表头与每一行的列数一致。
//
// 列数错位是 CSV 导出最隐蔽的错误：文件能打开，但"金额"列显示的是税号，
// 用户到对账时才发现。表头与 formatInvoiceExportRow 是两个必须同步改的地方。
func TestInvoiceExportHeader_MatchesRowWidth(t *testing.T) {
	row := formatInvoiceExportRow(InvoiceRequest{
		ID:          "inv-1",
		PaymentID:   "pay_1",
		Title:       "某某科技有限公司",
		TaxNo:       "91310000MA1K3XYZ12",
		AmountCents: 1234,
		Currency:    "CNY",
		Status:      invoiceStatusApproved,
		Note:        "请尽快",
		ReviewNote:  "已开票",
	})
	if len(row) != len(invoiceExportHeader) {
		t.Fatalf("row has %d columns, header has %d", len(row), len(invoiceExportHeader))
	}
}

// TestInvoiceExportRow_NeutralisesFormulaInjection 验证用户可控文本走 csvSafeText。
//
// 发票抬头与备注是**用户直接输入的**，`=cmd|calc` 这类内容在 Excel 里会被当公式
// 执行。金额两列则必须保持数值（否则 Excel 里再也算不了和），
// 与交易导出同一条取舍。
func TestInvoiceExportRow_NeutralisesFormulaInjection(t *testing.T) {
	row := formatInvoiceExportRow(InvoiceRequest{
		PaymentID:   "=cmd|calc",
		Title:       "@SUM(A1)",
		TaxNo:       "-1234",
		AmountCents: 1234,
		Currency:    "CNY",
		Status:      invoiceStatusPending,
		Note:        "+1+1",
		ReviewNote:  "",
	})

	// 列序见 invoiceExportHeader：0 时间 1 订单号 2 抬头 3 税号 4 金额(分) 5 金额 6 币种 7 状态 8 申请备注 9 审核备注 10 审核时间
	for _, idx := range []int{1, 2, 3, 8} {
		if !strings.HasPrefix(row[idx], "'") {
			t.Errorf("column %d (%q) must be prefixed to neutralise formula injection", idx, row[idx])
		}
	}
	// 金额两列必须是纯数值，不能被加前缀。
	if row[4] != "1234" {
		t.Errorf("amount cents must stay numeric, got %q", row[4])
	}
	if row[5] != "12.34" {
		t.Errorf("amount in yuan must stay numeric, got %q", row[5])
	}
}

// TestCentsToYuanString_KeepsCentsPrecise 验证分转元不引入浮点误差。
//
// 用整数运算而不是 `float64(cents)/100`：后者在 1234 这类值上正好，
// 但在 1005 / 9999999 等值上会出现 `10.049999999999999` 这种输出，
// 而金额显示错一分钱是用户会来投诉的问题。
func TestCentsToYuanString_KeepsCentsPrecise(t *testing.T) {
	cases := map[int64]string{
		0:         "0.00",
		1:         "0.01",
		10:        "0.10",
		100:       "1.00",
		1234:      "12.34",
		1005:      "10.05",
		9999999:   "99999.99",
		-1234:     "-12.34",
		100000000: "1000000.00",
	}
	for in, want := range cases {
		if got := centsToYuanString(in); got != want {
			t.Errorf("centsToYuanString(%d) = %q, want %q", in, got, want)
		}
	}
}

// TestInvoiceExportRow_TimeIsUTCAndOptionalColumnsMayBeEmpty 验证时间与时区。
//
// 时间列显式按 UTC 输出并在表头标注：跨时区对账时差几个小时，
// 用户没有任何线索知道差在哪。未审核的行没有审核时间，必须留空而不是 "-"。
func TestInvoiceExportRow_TimeIsUTCAndOptionalColumnsMayBeEmpty(t *testing.T) {
	shanghai := time.FixedZone("CST", 8*3600)
	created := time.Date(2026, 10, 29, 9, 2, 3, 0, shanghai) // = 01:02:03Z
	row := formatInvoiceExportRow(InvoiceRequest{
		CreatedAt:   &created,
		PaymentID:   "pay_1",
		Title:       "A",
		TaxNo:       "B",
		AmountCents: 100,
		Currency:    "CNY",
		Status:      invoiceStatusPending,
	})

	if row[0] != "2026-10-29T01:02:03Z" {
		t.Errorf("created_at must be rendered in UTC, got %q", row[0])
	}
	if row[10] != "" {
		t.Errorf("missing reviewed_at must stay empty (not \"-\"), got %q", row[10])
	}
	if row[8] != "" || row[9] != "" {
		t.Errorf("empty notes must stay empty, got %q / %q", row[8], row[9])
	}
}

// TestInvoiceExportDisposition_IsAttachmentWithASCIIName 验证下载文件名。
//
// 纯 ASCII 且不含冒号：中文文件名在部分浏览器会按错误编码落盘，
// 冒号在 Windows 文件系统上非法。平台与租户要能区分，否则运维会混。
func TestInvoiceExportDisposition_IsAttachmentWithASCIIName(t *testing.T) {
	platform := invoiceExportDisposition(0)
	if !strings.HasPrefix(platform, `attachment; filename="`) {
		t.Fatalf("must be an attachment: %q", platform)
	}
	if !strings.Contains(platform, "platform") {
		t.Errorf("platform export must be distinguishable: %q", platform)
	}
	if strings.Contains(platform, ":") {
		t.Errorf("filename must not contain a colon: %q", platform)
	}

	tenant := invoiceExportDisposition(7)
	if !strings.Contains(tenant, "tenant") {
		t.Errorf("tenant export must be distinguishable: %q", tenant)
	}
}

// TestWriteCSVRows_EmitsBOMHeaderAndGivenRows 验证通用写入器的输出形态。
//
// 发票导出复用这个函数：BOM（Excel 认 UTF-8）、CRLF（旧版 Excel 分行）、
// 逐行转义都必须一致。抽成 writeCSVRows 就是为了让两条导出链路共用同一份实现。
func TestWriteCSVRows_EmitsBOMHeaderAndGivenRows(t *testing.T) {
	header := []string{"a", "b"}
	rows := [][]string{{"1", "含,逗号"}, {"2", `含"引号`}}

	var buf bytes.Buffer
	seq := func(yield func([]string) bool) {
		for _, r := range rows {
			if !yield(r) {
				return
			}
		}
	}
	if err := writeCSVRows(&buf, header, seq); err != nil {
		t.Fatalf("writeCSVRows failed: %v", err)
	}

	out := buf.Bytes()
	if !bytes.HasPrefix(out, utf8BOM) {
		t.Fatalf("output must start with UTF-8 BOM")
	}
	// 去掉 BOM 再按标准 CSV 解析，验证转义与行数。
	r := csv.NewReader(bytes.NewReader(out[len(utf8BOM):]))
	parsed, err := r.ReadAll()
	if err != nil {
		t.Fatalf("output is not valid CSV: %v", err)
	}
	want := [][]string{header, rows[0], rows[1]}
	if !slices.EqualFunc(parsed, want, slices.Equal[[]string]) {
		t.Fatalf("unexpected csv:\n want %#v\n  got %#v", want, parsed)
	}
}
