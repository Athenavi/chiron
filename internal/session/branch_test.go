package session

import "testing"

// TestPlanBranch 钉住"裁剪计划"的语义 —— 这是分支功能最容易错的地方：
// 窗口起点（复制哪几条）与状态初值（要不要等压缩）。
//
// 语义（docs/session-map-branch-design.md 2.3）：
//
//	源会话  m1 ... m(N-K) | m(N-K+1) ... mN | m(N+1) ...
//	        └─ 压缩区 ──┘ └─ 原文保留 ──┘ └─ 被裁掉（丢弃）─┘
//	新会话  [模型压缩摘要] + m(N-K+1) ... mN
func TestPlanBranch(t *testing.T) {
	cases := []struct {
		name      string
		mode      string
		fromIndex int
		keepTail  int
		want      BranchPlan
		wantErr   bool
	}{
		{
			name: "默认 condense：前面 8 条进压缩区，尾部 4 条留原文，等待压缩",
			mode: "", fromIndex: 12, keepTail: 0,
			want: BranchPlan{Mode: "condense", Offset: 8, Limit: 4, State: "pending", Condensed: true},
		},
		{
			name: "自定义 keep_tail",
			mode: "condense", fromIndex: 10, keepTail: 3,
			want: BranchPlan{Mode: "condense", Offset: 7, Limit: 3, State: "pending", Condensed: true},
		},
		{
			name: "压缩区不足 3 条：整段复制、直接 ready（不花模型钱）",
			mode: "condense", fromIndex: 5, keepTail: 4,
			want: BranchPlan{Mode: "condense", Offset: 0, Limit: 5, State: "ready"},
		},
		{
			name: "keep_tail 超过 from_index 时收敛（不能把窗口推成负数）",
			mode: "condense", fromIndex: 3, keepTail: 10,
			want: BranchPlan{Mode: "condense", Offset: 0, Limit: 3, State: "ready"},
		},
		{
			name: "truncate 保持旧行为：复制前 N 条、没有压缩状态",
			mode: "truncate", fromIndex: 7, keepTail: 4,
			want: BranchPlan{Mode: "truncate", Offset: 0, Limit: 7},
		},
		{name: "from_index 必须为正", mode: "condense", fromIndex: 0, wantErr: true},
		{name: "keep_tail 不能为负", mode: "condense", fromIndex: 10, keepTail: -1, wantErr: true},
		{name: "未知模式被拒（不能悄悄退化成某个语义）", mode: "compact", fromIndex: 10, wantErr: true},
	}

	for _, c := range cases {
		got, err := planBranch(c.mode, c.fromIndex, c.keepTail)
		if c.wantErr {
			if err == nil {
				t.Errorf("%s: 期望报错，实际返回 %+v", c.name, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("%s: 意外报错 %v", c.name, err)
			continue
		}
		if got != c.want {
			t.Errorf("%s:\n  got  %+v\n  want %+v", c.name, got, c.want)
		}
	}
}
