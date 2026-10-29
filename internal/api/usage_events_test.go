package api

import (
	"context"
	"testing"

	"github.com/athenavi/chiron/internal/db"
)

// 用量明细（usage_events）的入参与参数校验。
//
// 为什么值得单独测：`GetUsage` 同时服务两个数据源（credit_transactions 的记账流水
// 与 usage_events 的真实用量），参数一旦解析错，前端拿到的是**另一个维度**的数据
// 却毫无察觉 —— 用户看到"本月花费"其实是"本月充值"，比报错更糟。

func TestParseUsageParamsDefaults(t *testing.T) {
	// 省略参数时的行为必须与改造前完全一致，否则老前端会静默换口径。
	groupBy, days, errMsg := parseUsageParams("", "")
	if errMsg != "" {
		t.Fatalf("空参数应取默认值，不应报错，得到 %q", errMsg)
	}
	if groupBy != "day" {
		t.Fatalf("默认分组应为 day，得到 %q", groupBy)
	}
	if days != 30 {
		t.Fatalf("默认区间应为 30 天，得到 %d", days)
	}
}

func TestParseUsageParamsAcceptsWhitelist(t *testing.T) {
	cases := []struct {
		groupBy string
		rangeK  string
		wantDay int
	}{
		{"day", "7d", 7},
		{"model", "30d", 30},
		{"session", "90d", 90},
	}
	for _, c := range cases {
		gotGroup, gotDays, errMsg := parseUsageParams(c.groupBy, c.rangeK)
		if errMsg != "" {
			t.Fatalf("%s/%s 应合法，得到错误 %q", c.groupBy, c.rangeK, errMsg)
		}
		if gotGroup != c.groupBy || gotDays != c.wantDay {
			t.Fatalf("%s/%s 解析为 %q/%d，期望 %q/%d",
				c.groupBy, c.rangeK, gotGroup, gotDays, c.groupBy, c.wantDay)
		}
	}
}

func TestParseUsageParamsRejectsUnknownGroupBy(t *testing.T) {
	// 关键：必须**报错**而不是静默回落成 day。静默回落会让调用方以为
	// "按模型查询"生效了，实际拿到的却是按日流水。
	for _, bad := range []string{"endpoint", "MODEL", "day ", "1; DROP TABLE usage_events"} {
		_, _, errMsg := parseUsageParams(bad, "")
		if errMsg == "" {
			t.Fatalf("非法 groupBy %q 必须报错，实际被接受了", bad)
		}
	}
}

func TestParseUsageParamsRejectsUnknownRange(t *testing.T) {
	// 任意天数会让同步端点变成全表扫描，所以只允许白名单里的三档。
	for _, bad := range []string{"3650d", "1", "30", "0d", "-7d"} {
		_, _, errMsg := parseUsageParams("day", bad)
		if errMsg == "" {
			t.Fatalf("非法 range %q 必须报错，实际被接受了", bad)
		}
	}
}

func TestUsageGroupColumnMapsOnlyBreakdownDimensions(t *testing.T) {
	// day 走的是 credit_transactions 路径，不应有 SQL 列映射；
	// 若这里给 day 也返回了列，说明两条数据源的口径被混淆了。
	if col, ok := usageGroupColumn("day"); ok {
		t.Fatalf("day 分组不应有 usage_events 列映射，得到 %q", col)
	}
	for _, dim := range []string{"model", "session"} {
		col, ok := usageGroupColumn(dim)
		if !ok || col == "" {
			t.Fatalf("%s 应有列映射，得到 %q/%v", dim, col, ok)
		}
	}
}

func TestUsageGroupColumnKeepsEmptyValuesAsUnknown(t *testing.T) {
	// 空模型名必须归入 unknown 而不是被过滤掉，否则"各分组之和 < 总量"，
	// 用户会以为统计漏数据。
	col, ok := usageGroupColumn("model")
	if !ok {
		t.Fatal("model 应有列映射")
	}
	want := "COALESCE(NULLIF(model, ''), 'unknown')"
	if col != want {
		t.Fatalf("空模型名应归入 unknown，期望 %q，得到 %q", want, col)
	}
}

func TestRecordUsageEventIsNoopWithoutDatabase(t *testing.T) {
	// 明细写入是"只记录不阻塞"的路径：数据库不可用时必须静默返回，
	// 绝不能让一次已完成的对话因统计失败而报错。
	if db.GlobalDBManager != nil {
		t.Skip("本用例只在无数据库连接的单元测试环境有意义")
	}
	// 不 panic、不阻塞即为通过。
	RecordUsageEvent(context.Background(), UsageEvent{
		TenantID: 1, UserID: "u1", SessionID: "s1", Model: "gpt-x",
		InputTokens: 10, OutputTokens: 5,
	})
}

func TestRecordUsageEventSkipsEmptyEvents(t *testing.T) {
	// 全空事件不该产生一行记录：没有任何信息量的行会污染统计基数。
	// 这里同样依赖无 DB 环境（函数在入口处就返回），主要保证不会误写。
	if db.GlobalDBManager != nil {
		t.Skip("本用例只在无数据库连接的单元测试环境有意义")
	}
	RecordUsageEvent(context.Background(), UsageEvent{})
}
