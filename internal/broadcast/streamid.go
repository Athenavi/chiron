package broadcast

import (
	"strconv"
	"strings"
)

// ParseStreamID 解析 Redis Stream ID（`<ms>-<seq>`）。
//
// SSE 的事件 ID 就是 XADD 返回的流 ID（见 hub.go）。它是**两段十进制数**，不是字符串：
// 直接按字符串比较会在 seq 位数不同时得出错误结论（`"…-9"` 在字符串上大于 `"…-10"`）。
func ParseStreamID(id string) (ms int64, seq int64, ok bool) {
	dash := strings.IndexByte(id, '-')
	if dash <= 0 || dash == len(id)-1 {
		return 0, 0, false
	}
	ms, err := strconv.ParseInt(id[:dash], 10, 64)
	if err != nil || ms < 0 {
		return 0, 0, false
	}
	seq, err = strconv.ParseInt(id[dash+1:], 10, 64)
	if err != nil || seq < 0 {
		return 0, 0, false
	}
	return ms, seq, true
}

// IsNewerStreamID 报告 id 是否**严格晚于** last —— 用于"补发与实时重叠"窗口的去重。
//
// 规则遵循「宁可重复，不可丢弃」：
//   - last 为空或不可解析（例如客户端送来的伪造 Last-Event-ID）⇒ 基线不可信，一律视为新；
//   - id 不可解析 ⇒ 同样视为新（宁可在客户端多去重一次，也不静默丢事件）；
//   - 两者都可解析 ⇒ 按 (ms, seq) **数值**比较。
//
// 为什么不直接比字符串：Redis 的 sequence 是变宽十进制（同一毫秒内 `-9` 之后是 `-10`），
// 字符串比较会认为 `"…-9" > "…-10"`，于是**新事件被当成旧的丢掉**。更糟的是客户端只要传一个
// 非法 ID（如 `zzz`），所有真实流 ID 都比它"小" —— 连接保持打开、却再也不推任何事件。
func IsNewerStreamID(id, last string) bool {
	if last == "" {
		return true
	}
	idMS, idSeq, idOK := ParseStreamID(id)
	lastMS, lastSeq, lastOK := ParseStreamID(last)
	if !idOK || !lastOK {
		return true // 基线不可信：不丢事件
	}
	if idMS != lastMS {
		return idMS > lastMS
	}
	return idSeq > lastSeq
}
