package api

import (
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"iter"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// billing_export.go —— 交易明细 CSV 导出（P1-1 / roadmap §10.2）。
//
// 为什么不用现成的 GetHistory：它是 `ORDER BY created_at DESC LIMIT 50` 且不接受
// 时间范围与分页参数，是给「页面看最近 50 条」用的。导出要的是**按时间范围全量**
// 与稳定正序，两者查询形态不同，硬复用只会让两边都别扭。
//
// 为什么同步流式而不是异步任务：仓库没有可复用的统一任务队列（`asynq|Enqueue`
// 的命中分散在无关模块），为一个导出建队列是净负债。5 万行的上限内，流式写出
// 的耗时与内存都可控；真超过再单独评估异步。

const (
	// exportMaxRows 是单次导出的行数上限。
	//
	// 有上限而不是"要多少给多少"：这个端点是**同步**运行的，一个 10 万行的账号
	// 会让连接被占住几十秒，而用户点导出往往是在对账，不是在做压力测试。
	// 超限时明确报错并让他缩小时间范围，比静默截断（用户以为导全了）安全得多。
	exportMaxRows = 50000

	// exportMaxSpanDays 是时间范围的跨度上限，与前端可选的 90 天同量级。
	// 放一年不是为了"允许导一年"，而是给按月对账留出余量。
	exportMaxSpanDays = 366
)

// utf8BOM 是 UTF-8 的字节序标记。
//
// Excel 打开无 BOM 的 UTF-8 CSV 时按本地代码页解释，中文会变成乱码 ——
// 这是"导出功能看起来能用、实际不能用"的经典死法，用户通常只会说"导出来是乱码"。
var utf8BOM = []byte{0xEF, 0xBB, 0xBF}

// exportCSVHeader 是表头。
//
// 时间列显式标注 UTC：`credit_transactions.created_at` 是 `timestamp`（无时区，
// 见 roadmap §10.2 与 D 组 L4-3），pgx 扫出来按 UTC 解释。不标清楚，跨时区
// 对账会差几个小时，而用户没有任何线索知道差在哪。
var exportCSVHeader = []string{"时间(UTC)", "事由", "金额", "余额快照", "关联轮次"}

// errExportRangeTooWide 表示请求的时间范围超过上限。
var errExportRangeTooWide = errors.New(
	fmt.Sprintf("time range spans more than %d days", exportMaxSpanDays))

// exportRow 是一条待导出的流水。
//
// 不复用 billing.CreditChange：它没有 turn_id 字段，而导出要带上"关联轮次"；
// 为了一个导出往公共结构体加字段，会让 GetHistory 的 JSON 契约也发生变化。
type exportRow struct {
	CreatedAt time.Time
	Reason    string
	Amount    int
	Balance   int
	TurnID    string
}

// errExportTooManyRows 表示范围内的流水数超过导出上限。
type exportTooManyRowsError struct{ Total int }

func (e *exportTooManyRowsError) Error() string {
	return fmt.Sprintf("too many transactions (%d) in range, please narrow the date range", e.Total)
}

// checkExportRowLimit 判定范围内的行数是否可导出。
//
// 抽成纯函数而不是内联在 handler 里：这是"超限报错而非静默截断"这条产品承诺
// 的唯一落点，必须能脱离数据库测试。边界取 `>`（恰好 50000 行可导）：
// 拿到 limit 行且 count 等于 limit 时，数据其实是完整的，此时报错属于误伤。
func checkExportRowLimit(total int) error {
	if total > exportMaxRows {
		return &exportTooManyRowsError{Total: total}
	}
	return nil
}

// exportCountQuery / exportRowsQuery 是导出的两条查询。
//
// 提成包级常量是为了让"用户隔离"与"口径一致"可被测试断言，而不是只存在于
// 一段没人敢改的字符串里：两条查询必须都带 `user_id = $1`（隔离靠它，且
// user_id 只能来自鉴权 claims）、都排除 free_chat（与 GetHistory 同口径）。
//
// 参数顺序：$1=userID $2=from $3=to（行查询另有 $4=limit）。
const exportCountQuery = `SELECT count(*) FROM credit_transactions
	  WHERE user_id = $1 AND reason <> 'free_chat'
	    AND created_at >= $2 AND created_at <= $3`

const exportRowsQuery = `SELECT amount, balance, reason, created_at, COALESCE(turn_id, '')
	   FROM credit_transactions
	  WHERE user_id = $1 AND reason <> 'free_chat'
	    AND created_at >= $2 AND created_at <= $3
	  ORDER BY created_at ASC, id ASC
	  LIMIT $4`

// parseExportRange 解析并校验导出的时间范围。
//
// 两个参数都可省略：省 from 默认近 30 天，省 to 默认此刻。与 `/v1/billing/usage`
// 的 range 白名单同一个思路 —— 不让调用方用参数制造无边界的全表扫描。
func parseExportRange(fromRaw, toRaw string, now time.Time) (from, to time.Time, err error) {
	to = now
	if toRaw != "" {
		parsed, perr := time.Parse(time.RFC3339, toRaw)
		if perr != nil {
			return time.Time{}, time.Time{}, errors.New("to must be RFC3339")
		}
		to = parsed
	}

	from = to.AddDate(0, 0, -30)
	if fromRaw != "" {
		parsed, perr := time.Parse(time.RFC3339, fromRaw)
		if perr != nil {
			return time.Time{}, time.Time{}, errors.New("from must be RFC3339")
		}
		from = parsed
	}

	if to.Before(from) {
		return time.Time{}, time.Time{}, errors.New("to must be after from")
	}
	if to.Sub(from) > exportMaxSpanDays*24*time.Hour {
		return time.Time{}, time.Time{}, errExportRangeTooWide
	}
	return from, to, nil
}

// csvSafeText 对**文本**字段做公式注入防护。
//
// 为什么只有文本字段走这里、金额不走：Excel 会把以 `=`/`+`/`-`/`@` 开头的单元格
// 当公式执行（CSV injection）。金额天然可能是负数（`-42`），加前缀会把它变成
// 文本、Excel 里再也算不了和。而 `reason` 是内部枚举，正常取值不会以这些字符开头，
// 这里只是防御将来接入用户可控文本。
func csvSafeText(s string) string {
	if s == "" {
		return s
	}
	switch s[0] {
	case '=', '+', '-', '@', '\t', '\r':
		return "'" + s
	}
	return s
}

// formatTransactionRecord 把一条流水转成 CSV 行。
//
// 顺序必须与 exportCSVHeader 一致；金额与余额是数值，直接 strconv 而不做
// csvSafeText（理由同上）。turn_id 可能为空，输出空字符串而不是 "-"，
// 避免把"没有关联轮次"和"关联轮次叫 -"混为一谈。
func formatTransactionRecord(row exportRow) []string {
	return []string{
		row.CreatedAt.UTC().Format(time.RFC3339),
		csvSafeText(row.Reason),
		strconv.Itoa(row.Amount),
		strconv.Itoa(row.Balance),
		row.TurnID,
	}
}

// writeCSV 写出 BOM、表头与全部数据行。
//
// 收 iter.Seq 而不是 []exportRow：handler 里数据是一边查一边扫的，用迭代器
// 可以让「CSV 长什么样」只有一份实现 —— 测试传切片转成的序列，handler 传
// 数据库游标，两者走的是同一段代码。若写成两次循环，BOM/表头/转义任何一处
// 改动都可能只改到一边。
func writeCSV(w io.Writer, rows iter.Seq[exportRow]) error {
	// BOM 必须先于任何其它字节写出，否则 Excel 认不出来（它只看文件开头）。
	if _, err := w.Write(utf8BOM); err != nil {
		return err
	}

	cw := csv.NewWriter(w)
	// RFC 4180 用 CRLF 作行结束符；Excel 与大多数表格软件都认它。
	cw.UseCRLF = true

	if err := cw.Write(exportCSVHeader); err != nil {
		return err
	}

	// 能 flush 就逐行 flush：用户看到的是"正在下载"，而不是转圈等到全量查完。
	flusher, canFlush := w.(http.Flusher)
	for row := range rows {
		if err := cw.Write(formatTransactionRecord(row)); err != nil {
			return err
		}
		if canFlush {
			cw.Flush()
			flusher.Flush()
		}
	}
	cw.Flush()
	return cw.Error()
}

// exportDisposition 生成 Content-Disposition，文件名带上时间范围。
//
// 用 `T` 分隔日期与时间：冒号在 Windows 文件系统上非法，用户下载后改名会很烦。
// 文件名只用 ASCII，避免某些浏览器把中文文件名按错误编码落盘。
func exportDisposition(from, to time.Time) string {
	name := "transactions_" +
		from.UTC().Format("20060102T1504") + "_" +
		to.UTC().Format("20060102T1504") + ".csv"
	return `attachment; filename="` + name + `"`
}

// ExportTransactions 导出当前用户的交易明细。
//
// GET /v1/billing/transactions.csv?from=<RFC3339>&to=<RFC3339>
//
// 只导出**当前登录用户自己的**流水：user_id 来自鉴权 claims，不接受参数覆盖。
// free_chat 记录被排除，与 GetHistory 同口径 —— 否则用户会发现"页面上 50 条"
// 与"导出的内容"对不上，而两边其实都没错。
func (h *BillingHandler) ExportTransactions(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	from, to, err := parseExportRange(
		r.URL.Query().Get("from"),
		r.URL.Query().Get("to"),
		time.Now(),
	)
	if err != nil {
		// 范围过宽是用户可自行解决的问题，单独给码 + 上限参数：
		// 否则前端只能把英文原文摆给中文用户，他也不知道"多宽算宽"。
		if errors.Is(err, errExportRangeTooWide) {
			JSONWithCode(w, http.StatusBadRequest, CodeExportRangeTooWide,
				map[string]interface{}{"max_days": exportMaxSpanDays}, err.Error())
			return
		}
		BadRequest(w, err.Error())
		return
	}

	// 先数一遍再写。顺序很关键：一旦开始往 w 写字节，状态码就固定为 200，
	// 超限时就没法再返回 400 了。所以"能不能导"必须在写第一个字节之前定下来。
	var total int
	if err := db.GlobalDBManager.QueryRow(r.Context(), exportCountQuery,
		userID, from, to).Scan(&total); err != nil {
		slog.Error("count transactions for export failed",
			"user_id", userID, "error", err, "db_available", db.GlobalDBManager.IsAvailable())
		InternalError(w, "failed to export transactions")
		return
	}
	if err := checkExportRowLimit(total); err != nil {
		// 带 code + params：前端据此渲染"请缩小时间范围（共 N 条，上限 M 条）"。
		// 只给英文原文的话，中文用户看到的是没法读的信息。
		var tooMany *exportTooManyRowsError
		if errors.As(err, &tooMany) {
			JSONWithCode(w, http.StatusBadRequest, CodeExportTooManyRows,
				map[string]interface{}{
					"total": tooMany.Total,
					"limit": exportMaxRows,
				}, err.Error())
			return
		}
		BadRequest(w, err.Error())
		return
	}

	rows, err := db.GlobalDBManager.Query(r.Context(), exportRowsQuery,
		userID, from, to, exportMaxRows)
	if err != nil {
		slog.Error("query transactions for export failed", "user_id", userID, "error", err)
		InternalError(w, "failed to export transactions")
		return
	}
	defer rows.Close()

	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Disposition", exportDisposition(from, to))

	// 流式写出：每行扫完立刻交给 writeCSV，内存占用不随行数增长。
	// 客户端断开时 writeCSV 会提前返回（yield 返回 false），此时不再查剩下的行。
	seq := func(yield func(exportRow) bool) {
		for rows.Next() {
			var row exportRow
			if err := rows.Scan(&row.Amount, &row.Balance, &row.Reason,
				&row.CreatedAt, &row.TurnID); err != nil {
				// 单行扫描失败不该毁掉整次导出：跳过并留日志，用户拿到的是
				// "少一行"而不是"什么都没有"。
				slog.Warn("scan transaction row for export skipped",
					"user_id", userID, "error", err)
				continue
			}
			if !yield(row) {
				return
			}
		}
	}
	if err := writeCSV(w, seq); err != nil {
		slog.Warn("write transactions csv aborted", "user_id", userID, "error", err)
		return
	}
	if err := rows.Err(); err != nil {
		// 此时响应头已发出、状态码已是 200，无法再改成 5xx。
		// 记日志是唯一能做的事；客户端会看到一个被截断但语法完整的 CSV。
		slog.Error("iterate transactions for export failed",
			"user_id", userID, "error", err)
	}
}
