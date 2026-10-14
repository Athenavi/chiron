package api

import (
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/agentgoal"
)

func TestGoalEnforcePromptIncludesReasons(t *testing.T) {
	rep := &agentgoal.Report{
		Outcome: agentgoal.Incomplete,
		Reasons: []string{
			"verified-changes: 1 change(s) were not followed by a successful verification",
		},
	}
	p := goalEnforcePrompt(rep)
	if !strings.Contains(p, "verified-changes") {
		t.Fatalf("prompt must carry the owed reasons; got %q", p)
	}
	if !strings.Contains(p, "系统自动续跑") {
		t.Fatalf("prompt must say it is a system continuation, not a user question; got %q", p)
	}
}

func TestGoalEnforcePromptHandlesNil(t *testing.T) {
	p := goalEnforcePrompt(nil)
	if strings.TrimSpace(p) == "" {
		t.Fatalf("prompt must never be empty")
	}
}

func TestWithGoalEnforceDoesNotMutateInput(t *testing.T) {
	in := map[string]interface{}{"kb_id": "kb1"}
	out := withGoalEnforce(in)
	if _, leaked := in[GoalEnforceKey]; leaked {
		t.Fatalf("withGoalEnforce must not mutate the caller's map")
	}
	if out[GoalEnforceKey] != true {
		t.Fatalf("withGoalEnforce must set the marker")
	}
	if out["kb_id"] != "kb1" {
		t.Fatalf("withGoalEnforce must preserve existing keys")
	}
}

func TestGoalEnforceSourceDistinctFromFollowup(t *testing.T) {
	if GoalEnforceSource == FollowupSource {
		t.Fatalf("continuation source must be distinguishable from subagent followup")
	}
}

func TestGoalRoundsAreBounded(t *testing.T) {
	// P6 起续跑由 Goal FSM 驱动；默认回合上限必须是小而明确的界。
	if n := agentgoal.DefaultGoalRounds; n < 1 || n > 5 {
		t.Fatalf("default goal rounds must be a small bound, got %d", n)
	}
}
