package db

import (
	"context"
	"errors"
	"time"
)

// naive timestamp 列的**语义风险**诊断（L4-3 的前置）。
//
// 背景：库里 142 个 `timestamp without time zone` 列（72 张表）的**时区语义取决于写入路径**，
// 而有两条路径：
//
//   - SQL 里的 `NOW()` / `CURRENT_TIMESTAMP`（95 处）→ 按**数据库会话时区**落成 naive 值；
//   - Go 里的 `time.Now()`（110 处，其中 6 处 `.UTC()`）→ 按**宿主进程时区**落成 naive 值。
//
// 两侧一致时（本机开发环境：会话时区与宿主都是 Asia/Shanghai）naive 值语义单一；
// **一旦不一致**（典型：容器 TZ=UTC 而数据库 `timezone` 仍是 Asia/Shanghai，或反之），
// 同一列里就会**混着两种语义** —— 此时任何单条 `ALTER … USING col AT TIME ZONE 'X'`
// 都必然把一半的行移错时刻。把迁移建在这个前提上之前，先让部署把这件事说清楚：
// 本诊断在启动时只读比对两侧偏移，不一致就打一条**显式告警**（不阻断启动）。

// HostTimezoneOffsetSec 返回宿主进程在 at 时刻的时区偏移（秒，东为正）。
func HostTimezoneOffsetSec(at time.Time) int {
	_, offset := at.Zone()
	return offset
}

// timezoneAligned 判定两侧偏移是否一致（抽出来是为了能单测这个判据本身）。
func timezoneAligned(dbOffsetSec, hostOffsetSec int) bool {
	return dbOffsetSec == hostOffsetSec
}

// SessionTimezoneOffsetSec 读数据库**会话时区**相对 UTC 的偏移（秒）。
//
// `now()::timestamp` 是按会话时区把 timestamptz 转成的 naive 值，
// `now() AT TIME ZONE 'UTC'` 是 UTC 的 naive 值，两者之差即会话偏移。
func SessionTimezoneOffsetSec(ctx context.Context) (int, error) {
	if Pool == nil {
		return 0, errors.New("database pool not initialized")
	}
	var secs int
	err := Pool.QueryRow(ctx,
		`SELECT EXTRACT(EPOCH FROM (now()::timestamp - (now() AT TIME ZONE 'UTC')))::int`,
	).Scan(&secs)
	return secs, err
}

// CheckTimezoneAlignment 比对数据库会话时区与宿主时区（只读）。
// 返回两侧偏移（秒）与是否一致；查询失败时 err 非 nil，由调用方决定是否告警。
func CheckTimezoneAlignment(ctx context.Context, at time.Time) (dbOffsetSec, hostOffsetSec int, aligned bool, err error) {
	dbOffsetSec, err = SessionTimezoneOffsetSec(ctx)
	if err != nil {
		return 0, 0, false, err
	}
	hostOffsetSec = HostTimezoneOffsetSec(at)
	return dbOffsetSec, hostOffsetSec, timezoneAligned(dbOffsetSec, hostOffsetSec), nil
}
