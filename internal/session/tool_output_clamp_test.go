package session

import "testing"

// tool_output_clamp_test.go —— 分段读取长度上限的收敛规则。
//
// 为什么它值得单独测：这个上限是「分段读取」不退化成「全量读取」的唯一闸门。
// 它同时被两处使用 —— `GetToolCallOutput` 下发的查询、以及 API 层回填给前端的
// `limit` 字段。后者是前端判断「是否还有下一段」的依据，两处一旦不一致，
// 前端就会按错误的 offset 继续请求，空转到超时。

func TestClampToolOutputLimitFallsBackToPreview(t *testing.T) {
	// 非正值一律当「没指定」：负数是客户端输入错误，不该被当成 0 长度读取。
	for _, in := range []int{0, -1, -4096} {
		if got := ClampToolOutputLimit(in); got != outputPreviewBytes {
			t.Errorf("ClampToolOutputLimit(%d) = %d, want %d (preview fallback)",
				in, got, outputPreviewBytes)
		}
	}
}

func TestClampToolOutputLimitPassesThroughNormalValues(t *testing.T) {
	for _, in := range []int{1, 100, 8192, 8191, toolOutputMaxChunk} {
		if got := ClampToolOutputLimit(in); got != in {
			t.Errorf("ClampToolOutputLimit(%d) = %d, want unchanged", in, got)
		}
	}
}

func TestClampToolOutputLimitCapsOversizedRequests(t *testing.T) {
	// 关键回归：客户端请求 1MB 时必须被夹到 64KB。否则一次请求就能把整条
	// output 拉走，分页省下的带宽又全花回去。
	for _, in := range []int{toolOutputMaxChunk + 1, 256_000, 1 << 20} {
		if got := ClampToolOutputLimit(in); got != toolOutputMaxChunk {
			t.Errorf("ClampToolOutputLimit(%d) = %d, want cap %d",
				in, got, toolOutputMaxChunk)
		}
	}
}

func TestClampToolOutputLimitIsMonotonic(t *testing.T) {
	// 单调不减：调大请求不该反而拿到更少。防止日后改成非单调查询（如按
	// "剩余字节"折算）时出现小请求拿得多、大请求拿得少的反直觉行为。
	//
	// 只在**正数范围**内检查：非正值是"未指定"的哨兵，回落成默认值 4096，
	// 与"传 1 就取 1 个字符"本就不同源，把它们放进同一条单调曲线里比较没有意义。
	prev := 0
	for in := 1; in <= toolOutputMaxChunk+10; in++ {
		got := ClampToolOutputLimit(in)
		if got < prev {
			t.Fatalf("not monotonic at %d: %d < previous %d", in, got, prev)
		}
		prev = got
	}
}

func TestClampToolOutputLimitSentinelIsNotZero(t *testing.T) {
	// 非正值回落成的默认值必须**大于零**：回落成 0 会发出 `substring(... for 0)`，
	// 一次读回空串，前端据此判定"已到末尾"，用户再也看不到全文。
	if got := ClampToolOutputLimit(0); got <= 0 {
		t.Fatalf("zero-limit fallback must be positive, got %d", got)
	}
	// 且该默认值不得超出硬上限，否则默认路径本身就能绕过夹取。
	if outputPreviewBytes > toolOutputMaxChunk {
		t.Fatalf("default %d exceeds cap %d", outputPreviewBytes, toolOutputMaxChunk)
	}
}
