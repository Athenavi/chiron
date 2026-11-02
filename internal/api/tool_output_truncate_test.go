package api

import (
	"strings"
	"testing"
	"unicode/utf8"
)

// 截断必须落在 UTF-8 字符边界上。按字节硬切会把一个多字节字符劈成两半，
// 整段文本在 psql / 前端渲染时变成 U+FFFD —— 比"结果被截短"更难排查。
func TestTruncateToolOutputKeepsRuneBoundaries(t *testing.T) {
	// 每个汉字 3 字节；limit=10 落在第 4 个汉字中间，必须回退到 9。
	text := strings.Repeat("汉", 100)

	got, original := truncateToolOutput(text, 10)

	if original != 300 {
		t.Fatalf("original bytes = %d, want 300", original)
	}
	if !utf8.ValidString(got) {
		t.Fatal("truncated output is not valid UTF-8")
	}
	if strings.ContainsRune(got, utf8.RuneError) {
		t.Fatal("truncated output contains U+FFFD")
	}
	body, _, found := strings.Cut(got, "\n\n[... output truncated")
	if !found {
		t.Fatal("truncation marker missing")
	}
	if len(body) > 10 {
		t.Fatalf("body = %d bytes, want <= 10", len(body))
	}
	if len(body)%3 != 0 {
		t.Fatalf("body = %d bytes, not a whole number of 3-byte runes", len(body))
	}
	if !strings.Contains(got, "300 bytes total") {
		t.Fatal("marker must report the original size, not the truncated one")
	}
}

func TestTruncateToolOutputPassesThroughAtOrUnderLimit(t *testing.T) {
	for _, tc := range []struct {
		name string
		text string
		max  int
	}{
		{"under", strings.Repeat("a", 99), 100},
		{"exactly at limit", strings.Repeat("a", 100), 100},
		{"empty", "", 100},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, original := truncateToolOutput(tc.text, tc.max)
			if got != tc.text {
				t.Fatal("text at or under the limit must pass through untouched")
			}
			if original != len(tc.text) {
				t.Fatalf("original = %d, want %d", original, len(tc.text))
			}
		})
	}
}

// limit<=0 不能 panic：负数切片会 panic，而 handler 的 recover 会吞掉整条工具记录。
func TestTruncateToolOutputToleratesNonPositiveLimit(t *testing.T) {
	text := strings.Repeat("x", 50)
	for _, limit := range []int{0, -1} {
		got, original := truncateToolOutput(text, limit)
		if got != text || original != 50 {
			t.Fatalf("limit=%d: got len=%d original=%d, want untouched 50", limit, len(got), original)
		}
	}
}

func TestTruncateToolOutputTruncatesASCII(t *testing.T) {
	text := strings.Repeat("a", 1000)
	got, original := truncateToolOutput(text, 512)

	if original != 1000 {
		t.Fatalf("original = %d, want 1000", original)
	}
	body, _, found := strings.Cut(got, "\n\n[... output truncated")
	if !found {
		t.Fatal("truncation marker missing")
	}
	if len(body) != 512 {
		t.Fatalf("body = %d bytes, want exactly 512 for ASCII", len(body))
	}
	if !utf8.ValidString(got) {
		t.Fatal("truncated output is not valid UTF-8")
	}
}
