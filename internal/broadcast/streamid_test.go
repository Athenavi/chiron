package broadcast

import "testing"

func TestParseStreamID(t *testing.T) {
	valid := []struct {
		in      string
		ms, seq int64
	}{
		{"1700000000000-0", 1700000000000, 0},
		{"1700000000000-15", 1700000000000, 15},
		{"1-1", 1, 1},
	}
	for _, tc := range valid {
		ms, seq, ok := ParseStreamID(tc.in)
		if !ok || ms != tc.ms || seq != tc.seq {
			t.Fatalf("ParseStreamID(%q) = (%d,%d,%v), want (%d,%d,true)", tc.in, ms, seq, ok, tc.ms, tc.seq)
		}
	}
	invalid := []string{"", "-", "1", "1-", "-1", "zzz", "1-2-3", "1-a", "a-1", "999999999999999999999999-0"}
	for _, in := range invalid {
		if _, _, ok := ParseStreamID(in); ok {
			t.Fatalf("ParseStreamID(%q) 不应被接受", in)
		}
	}
}

// 固化"为什么不能用字符串比"：同一毫秒内 seq 是变宽十进制，`"…-10"` 在字符串上**小于**
// `"…-9"` ⇒ 修复前的 `event.ID <= lastSentID` 会把**新**事件判成旧的并丢弃。
func TestLexicographicComparisonIsTheBug(t *testing.T) {
	newer, older := "1700000000000-10", "1700000000000-9"
	if !(newer <= older) {
		t.Fatalf("前提变了：字符串比较认为 %q 不晚于 %q", newer, older)
	}
	if !IsNewerStreamID(newer, older) {
		t.Fatalf("%q 在数值上晚于 %q，必须判为新事件", newer, older)
	}
}

func TestIsNewerStreamIDAcrossSequenceWidths(t *testing.T) {
	cases := []struct {
		id, last string
		want     bool
	}{
		{"1700000000000-10", "1700000000000-9", true},
		{"1700000000000-9", "1700000000000-10", false},
		{"1700000000000-9", "1700000000000-9", false},
		{"1700000000000-0", "1700000000000-0", false},
	}
	for _, tc := range cases {
		if got := IsNewerStreamID(tc.id, tc.last); got != tc.want {
			t.Errorf("IsNewerStreamID(%q, %q) = %v, want %v", tc.id, tc.last, got, tc.want)
		}
	}
}

func TestIsNewerStreamIDComparesMilliseconds(t *testing.T) {
	if !IsNewerStreamID("1000000000001-0", "1000000000000-999") {
		t.Fatal("毫秒更大即更新，即使 seq 更小")
	}
	if IsNewerStreamID("1000000000000-999", "1000000000001-0") {
		t.Fatal("毫秒更小即不更新")
	}
}

// 回归：客户端送来伪造/损坏的 Last-Event-ID 时，绝不能把所有真实事件判成"旧的" ——
// 那会让连接保持打开却再也不推任何事件（旧实现下 `"1700000000000-0" <= "zzz"` 恒真）。
func TestIsNewerStreamIDNeverDropsOnUnusableBaseline(t *testing.T) {
	for _, last := range []string{"", "zzz", "not-an-id", "999999999999999999999999-0"} {
		if !IsNewerStreamID("1700000000000-0", last) {
			t.Fatalf("基线 %q 不可用时必须视为新事件（宁可重复，不可丢弃）", last)
		}
	}
	if !IsNewerStreamID("garbage", "1700000000000-0") {
		t.Fatal("无法解析的 id 也应视为新事件")
	}
}
