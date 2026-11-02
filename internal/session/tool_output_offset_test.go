package session

import "testing"

// tool_output_offset_test.go —— 分段读取的「下一段从哪开始」。
//
// 这一组针对的是一个真实缺陷：offset 的口径。服务端 `substring(... for n)` 数的是
// **Unicode 码点**，而调用方（前端）拿到的内容长度往往按字节或 UTF-16 码元算。
// 只要用错口径，`已收内容的长度` 就不再等于「已消费的码点数」，下一段要么跳过
// 若干字符（用户看到的内容有空洞），要么在越界处原地打转（永远读不完）。
//
// 所以下一段起点一律由服务端算 —— 只有它知道自己切到哪了。

func TestNextToolOutputOffsetCountsRunesNotBytes(t *testing.T) {
	// 纯中文：3 个码点、9 个字节。按字节推进会让 offset 变成 9，
	// 下一段直接从末尾之后开始，"展开全文"只拿到第一段。
	next := NextToolOutputOffset(0, "中文段", 9)
	if next != 3 {
		t.Fatalf("NextToolOutputOffset(0, 中文段, 9) = %d, want 3（按码点，不是 %d 字节）",
			next, len("中文段"))
	}
}

func TestNextToolOutputOffsetHandlesSurrogatePairs(t *testing.T) {
	// emoji 是增补平面字符：1 个码点、4 个 UTF-8 字节、2 个 UTF-16 码元。
	// 三个口径三种答案，这里必须是码点数（2）。
	if next := NextToolOutputOffset(0, "😀😀", 2); next != 2 {
		t.Fatalf("emoji 段应推进 2 个码点，实际 %d", next)
	}
}

func TestNextToolOutputOffsetAccumulatesFromGivenOffset(t *testing.T) {
	// 起点由调用方给，结果必须是「起点 + 本段码点数」，供多次分段接续。
	if next := NextToolOutputOffset(10, "abc", 100); next != 13 {
		t.Fatalf("NextToolOutputOffset(10, abc, 100) = %d, want 13", next)
	}
}

func TestNextToolOutputOffsetClampsToTotalChars(t *testing.T) {
	// 读完最后一段时停在 totalChars，而不是「起点 + 段长」。
	// 前端据此判断 next_offset >= total_chars 即已读完。
	if next := NextToolOutputOffset(5, "abcdef", 8); next != 8 {
		t.Fatalf("越过末尾应夹到 totalChars=8，实际 %d", next)
	}
}

func TestNextToolOutputOffsetEmptyChunkDoesNotAdvance(t *testing.T) {
	// 空片段不得推进：否则前端在「服务端返回空串但未读完」时会跳过内容。
	if next := NextToolOutputOffset(7, "", 100); next != 7 {
		t.Fatalf("空片段不该推进 offset，实际 %d", next)
	}
}

func TestNextToolOutputOffsetNeverGoesBackwards(t *testing.T) {
	// 客户端传了越界 offset（比如 totalChars 被并发截断变小）时，
	// 不得返回比起点更小的值 —— 前端会把 pointer 往回拨，配合重试成死循环。
	if next := NextToolOutputOffset(50, "", 10); next < 50 {
		t.Fatalf("offset 不得后退：起点 50，实际返回 %d", next)
	}
}
