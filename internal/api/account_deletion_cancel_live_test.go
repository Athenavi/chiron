package api

import (
	"context"
	"net/http"
	"testing"

	"github.com/google/uuid"
)

// 冻结时必须打断该用户正在跑的回合。
//
// 不打断的后果：余额刚被清零，进行中的回合跑到下一次扣费就撞「余额不足」，
// 用户看到的是「点了注销之后突然报错」，而正确行为是「立即停止」。
//
// 用真实的 sessionCancels 条目驱动：它是「此刻正在跑的回合」的权威集合，
// cancelUserRunningTurns 正是遍历它。断言 cancel 函数被调用。
func TestLiveDeletionCancelsRunningTurns(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)
	h := mfaTestHandler(t)
	ctx := context.Background()

	// 清余额以满足注销门槛。
	if _, err := pool.Exec(ctx, `UPDATE users SET credits = 0 WHERE id = $1`, userID); err != nil {
		t.Fatalf("清零余额失败: %v", err)
	}

	// 造两个正在跑的会话：属于该用户（应被取消）与属于别人（不该被动）。
	sidMine := uuid.NewString()
	sidOther := uuid.NewString()
	canceledMine := false
	canceledOther := false
	sessionCancels.Store(sidMine, &sessionCancel{
		userID: userID,
		cancel: func() { canceledMine = true },
	})
	sessionCancels.Store(sidOther, &sessionCancel{
		userID: "someone-else",
		cancel: func() { canceledOther = true },
	})
	t.Cleanup(func() {
		sessionCancels.Delete(sidMine)
		sessionCancels.Delete(sidOther)
	})

	if rec := deletionRequest(t, h, userID, ""); rec.Code != http.StatusOK {
		t.Fatalf("申请注销失败: status=%d body=%s", rec.Code, rec.Body.String())
	}

	if !canceledMine {
		t.Fatal("该用户正在跑的回合没有被取消：用户会看到「注销后突然报余额不足」")
	}
	if canceledOther {
		t.Fatal("取消了别人的会话：注销只应影响自己的回合")
	}
	// 认领语义：条目应被移除，避免重复取消。
	if _, ok := sessionCancels.Load(sidMine); ok {
		t.Fatal("取消后应移除会话条目（cancelSessionRun 的原子认领语义）")
	}
}
