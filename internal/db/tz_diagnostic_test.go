package db

import (
	"testing"
	"time"
)

// L4-3 前置判据的单测：naive timestamp 列的语义是否会被"混着写"，
// 取决于**数据库会话时区**与**宿主时区**是否一致（见 tz_diagnostic.go 的说明）。
// 判据本身（偏移相等）必须精确 —— 它决定要不要在迁移动手前先修部署时区。
func TestHostTimezoneOffsetSec(t *testing.T) {
	cases := []struct {
		name string
		zone *time.Location
		want int
	}{
		{"东八区", time.FixedZone("CST", 8*3600), 8 * 3600},
		{"UTC", time.UTC, 0},
		{"西五区", time.FixedZone("EST", -5*3600), -5 * 3600},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			at := time.Date(2026, 10, 8, 20, 0, 0, 0, tc.zone)
			if got := HostTimezoneOffsetSec(at); got != tc.want {
				t.Fatalf("宿主偏移应为 %d，得到 %d", tc.want, got)
			}
		})
	}
}

func TestTimezoneAligned(t *testing.T) {
	if !timezoneAligned(8*3600, 8*3600) {
		t.Fatal("两侧同为 +08 时必须判为一致（naive 语义单一）")
	}
	if timezoneAligned(0, 8*3600) {
		t.Fatal("DB=UTC 而宿主=+08 必须判为不一致：同一列会混着两种语义，是迁移最易出错的场景")
	}
	if timezoneAligned(8*3600, 0) {
		t.Fatal("反向不一致同样必须被发现")
	}
}
