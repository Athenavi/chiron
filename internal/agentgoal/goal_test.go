package agentgoal

import "testing"

func TestAdvanceCompletedStops(t *testing.T) {
	g := NewGoal("g1", "s1", "do the thing", 100)
	g, cont := Advance(g, Report{Outcome: Completed}, 200)
	if cont {
		t.Fatalf("completed goal must not continue")
	}
	if g.Status != GoalCompleted {
		t.Fatalf("want completed, got %s", g.Status)
	}
	if g.UpdatedAt != 200 {
		t.Fatalf("timestamp must be host-written")
	}
}

func TestAdvanceIncompleteContinuesUntilBudget(t *testing.T) {
	g := NewGoal("g1", "s1", "x", 0)
	g.MaxRounds = 2

	// 第一次未完成 → 继续（RoundsUsed=1）
	g, cont := Advance(g, Report{Outcome: Incomplete, Reasons: []string{"owed"}}, 1)
	if !cont || g.Status != GoalActive || g.RoundsUsed != 1 {
		t.Fatalf("first incomplete should continue: %+v cont=%v", g, cont)
	}

	// 到上限 → blocked，不再继续
	g, cont = Advance(g, Report{Outcome: Incomplete, Reasons: []string{"owed"}}, 2)
	if cont {
		t.Fatalf("exhausted budget must not continue")
	}
	if g.Status != GoalBlocked {
		t.Fatalf("want blocked at budget end, got %s", g.Status)
	}
	if g.RoundsUsed != 2 {
		t.Fatalf("want rounds_used=2, got %d", g.RoundsUsed)
	}
}

func TestAdvanceFailedBlocksWithoutContinuing(t *testing.T) {
	g := NewGoal("g1", "s1", "x", 0)
	g, cont := Advance(g, Report{Outcome: Failed, Reasons: []string{"no-error: boom"}}, 1)
	if cont {
		t.Fatalf("failed turn must not auto-continue")
	}
	if g.Status != GoalBlocked {
		t.Fatalf("want blocked, got %s", g.Status)
	}
}

func TestAdvanceBlockedIsAbandoned(t *testing.T) {
	g := NewGoal("g1", "s1", "x", 0)
	g, cont := Advance(g, BlockedReport("deadline"), 1)
	if cont {
		t.Fatalf("cancelled turn must not continue")
	}
	if g.Status != GoalAbandoned {
		t.Fatalf("want abandoned, got %s", g.Status)
	}
}

func TestAdvanceNeverMarksCompletedOnIncomplete(t *testing.T) {
	// 核心纪律：没做完就是没做完 —— 绝不因为"到了上限"而谎报 completed。
	g := NewGoal("g1", "s1", "x", 0)
	g.MaxRounds = 1
	g, _ = Advance(g, Report{Outcome: Incomplete, Reasons: []string{"owed"}}, 1)
	if g.Status == GoalCompleted {
		t.Fatalf("incomplete must never become completed")
	}
}

func TestEffectiveMaxRoundsDefault(t *testing.T) {
	if NewGoal("g", "s", "d", 0).EffectiveMaxRounds() != DefaultGoalRounds {
		t.Fatalf("zero MaxRounds must fall back to the default")
	}
}

func TestTerminal(t *testing.T) {
	if !(Goal{Status: GoalCompleted}).Terminal() {
		t.Fatalf("completed is terminal")
	}
	if !(Goal{Status: GoalBlocked}).Terminal() {
		t.Fatalf("blocked is terminal")
	}
	if (Goal{Status: GoalActive}).Terminal() {
		t.Fatalf("active is not terminal")
	}
}
