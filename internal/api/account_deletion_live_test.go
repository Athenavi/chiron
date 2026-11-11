package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"golang.org/x/crypto/bcrypt"
)

// ── 账号注销：冻结 / 撤销（真库）──
//
// 这条链路的风险全在"半冻结"：
//   - 余额没归零 → 注销后仍在扣费；
//   - 定时任务没停 → 注销后还在跑、还在花额度；
//   - 撤销时笼统恢复 → 把用户自己停用的任务重新打开（开始扣费）。
//
// 上面每一条都只有跑一次真库才测得出来。取值口径与既有 *_live_test.go 一致：
// CHIRON_TEST_POSTGRES_DSN 未设即 skip。

// deletionRequest 走 HTTP 层申请注销（含密码确认）。
func deletionRequest(t *testing.T, h *AuthHandler, userID string, password string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/me/deletion",
		strings.NewReader(`{"confirm":true,"password":"`+password+`"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.RequestAccountDeletion(rec, req.WithContext(withUserCtx(req, userID, 1)))
	return rec
}

func deletionCancel(t *testing.T, h *AuthHandler, userID string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/me/deletion/cancel", nil)
	rec := httptest.NewRecorder()
	h.CancelAccountDeletion(rec, req.WithContext(withUserCtx(req, userID, 1)))
	return rec
}

// setUserPassword 给用户设一个已知密码（bcrypt MinCost：测试只验能否比对通过）。
func setUserPassword(t *testing.T, userID, plain string) {
	t.Helper()
	hash, err := bcrypt.GenerateFromPassword([]byte(plain), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("生成密码哈希失败: %v", err)
	}
	if _, err := withP12Pool(t).Exec(context.Background(),
		`UPDATE users SET password_hash = $2, credits = 500 WHERE id = $1`, userID, hash); err != nil {
		t.Fatalf("设置密码失败: %v", err)
	}
}

// 申请注销：余额冻结并归零、API Key 吊销、定时任务停用、到期时间落库。
func TestLiveDeletionRequestFreezesAssets(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)
	setUserPassword(t, userID, "correct-password")
	h := mfaTestHandler(t)
	ctx := context.Background()

	// 造两个资产：一条 API Key、一个定时任务。
	keyID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO api_keys (id, user_id, name, key_hash, created_at, revoked)
		 VALUES ($1, $2, 'k', 'hash', NOW(), false)`, keyID, userID); err != nil {
		t.Fatalf("插入 API Key 失败: %v", err)
	}
	cronID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO cron_jobs (id, name, schedule, task, enabled, tenant_id, user_id)
		 VALUES ($1, 'job', '0 3 * * *', 't', true, 1, $2)`, cronID, userID); err != nil {
		t.Fatalf("插入定时任务失败: %v", err)
	}

	rec := deletionRequest(t, h, userID, "correct-password")
	if rec.Code != http.StatusOK {
		t.Fatalf("申请注销失败: status=%d body=%s", rec.Code, rec.Body.String())
	}

	// ① 状态与余额
	var status string
	var credits int
	var frozen *int
	var dueAt *time.Time
	if err := pool.QueryRow(ctx,
		`SELECT account_status, credits, credits_frozen, deletion_due_at
		   FROM users WHERE id = $1`, userID).Scan(&status, &credits, &frozen, &dueAt); err != nil {
		t.Fatalf("读回用户状态失败: %v", err)
	}
	if status != accountStatusPendingDeletion {
		t.Fatalf("状态应为 pending_deletion，得到 %q", status)
	}
	if credits != 0 {
		t.Fatalf("credits 必须归零（否则并发回合仍能扣费），得到 %d", credits)
	}
	if frozen == nil || *frozen != 500 {
		t.Fatalf("余额应冻结为 500，得到 %v", frozen)
	}
	if dueAt == nil {
		t.Fatal("deletion_due_at 未落库")
	}
	// 宽限期约 7 天（留 1 分钟余量，避免测试机器时钟抖动）。
	if d := time.Until(*dueAt); d < 6*24*time.Hour+23*time.Hour || d > 7*24*time.Hour+time.Minute {
		t.Fatalf("到期时间距现在 %v，应接近 7 天", d)
	}

	// ② API Key 必须已吊销
	var revoked bool
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(revoked, false) FROM api_keys WHERE id = $1`, keyID).Scan(&revoked); err != nil {
		t.Fatalf("读回 API Key 失败: %v", err)
	}
	if !revoked {
		t.Fatal("API Key 未吊销：注销后仍可用脚本访问")
	}

	// ③ 定时任务必须已停用
	var enabled bool
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(enabled, false) FROM cron_jobs WHERE id = $1`, cronID).Scan(&enabled); err != nil {
		t.Fatalf("读回定时任务失败: %v", err)
	}
	if enabled {
		t.Fatal("定时任务未停用：注销后仍会到点跑并消耗额度")
	}
}

// 密码错误必须拒绝，且**不得**改变任何状态。
func TestLiveDeletionRequestRejectsBadPassword(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)
	setUserPassword(t, userID, "correct-password")
	h := mfaTestHandler(t)

	rec := deletionRequest(t, h, userID, "wrong-password")
	if rec.Code == http.StatusOK {
		t.Fatal("错误密码竟然通过了注销申请")
	}

	var status string
	var credits int
	if err := pool.QueryRow(context.Background(),
		`SELECT account_status, credits FROM users WHERE id = $1`, userID).
		Scan(&status, &credits); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if status != accountStatusActive || credits != 500 {
		t.Fatalf("拒绝后不应改状态：status=%q credits=%d", status, credits)
	}
}

// 未传 confirm 时必须拒绝：冻结资产是破坏性动作，不能由误触触发。
func TestLiveDeletionRequestRequiresConfirm(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)
	h := mfaTestHandler(t)

	req := httptest.NewRequest(http.MethodPost, "/v1/me/deletion",
		strings.NewReader(`{"confirm":false}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.RequestAccountDeletion(rec, req.WithContext(withUserCtx(req, userID, 1)))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("缺 confirm 应 400，得到 %d", rec.Code)
	}
	var status string
	if err := pool.QueryRow(context.Background(),
		`SELECT account_status FROM users WHERE id = $1`, userID).Scan(&status); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if status != accountStatusActive {
		t.Fatalf("未确认却改了状态：%q", status)
	}
}

// 撤销：余额恢复；**只**恢复当初被停用的对象，不碰用户自己停用的。
//
// 最后一条是本用例的重点：笼统 `enabled = true` 会把用户早就关掉的任务
// 重新打开、开始扣费，而用户根本不知道发生了什么。
func TestLiveDeletionCancelRestoresOnlyWhatWasFrozen(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)
	setUserPassword(t, userID, "correct-password")
	h := mfaTestHandler(t)
	ctx := context.Background()

	// 两个任务：一个启用（会被冻结），一个本来就停用（不该被碰）。
	activeJob := uuid.NewString()
	alreadyOffJob := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO cron_jobs (id, name, schedule, task, enabled, tenant_id, user_id)
		 VALUES ($1, 'a', '0 3 * * *', 't', true, 1, $2),
		        ($3, 'b', '0 4 * * *', 't', false, 1, $2)`,
		activeJob, userID, alreadyOffJob); err != nil {
		t.Fatalf("插入定时任务失败: %v", err)
	}

	if rec := deletionRequest(t, h, userID, "correct-password"); rec.Code != http.StatusOK {
		t.Fatalf("申请注销失败: %s", rec.Body.String())
	}
	if rec := deletionCancel(t, h, userID); rec.Code != http.StatusOK {
		t.Fatalf("撤销失败: status=%d body=%s", rec.Code, rec.Body.String())
	}

	var status string
	var credits int
	var frozen *int
	if err := pool.QueryRow(ctx,
		`SELECT account_status, credits, credits_frozen FROM users WHERE id = $1`, userID).
		Scan(&status, &credits, &frozen); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if status != accountStatusActive {
		t.Fatalf("撤销后应回到 active，得到 %q", status)
	}
	if credits != 500 {
		t.Fatalf("余额应恢复为 500，得到 %d", credits)
	}
	if frozen != nil {
		t.Fatal("credits_frozen 应被清空")
	}

	var activeNow, offNow bool
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(enabled, false) FROM cron_jobs WHERE id = $1`, activeJob).Scan(&activeNow); err != nil {
		t.Fatalf("读回任务失败: %v", err)
	}
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(enabled, false) FROM cron_jobs WHERE id = $1`, alreadyOffJob).Scan(&offNow); err != nil {
		t.Fatalf("读回任务失败: %v", err)
	}
	if !activeNow {
		t.Fatal("原本启用的任务应被恢复为启用")
	}
	if offNow {
		t.Fatal("用户自己停用的任务被重新打开了：撤销不该改变用户既有配置")
	}
}

// 租户所有者不能自行注销（会让租户失去管理者且数据无主）。
func TestLiveDeletionRejectsTenantOwner(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)
	if _, err := pool.Exec(context.Background(),
		`UPDATE users SET role = 'owner' WHERE id = $1`, userID); err != nil {
		t.Fatalf("设置角色失败: %v", err)
	}
	h := mfaTestHandler(t)

	// owner 未设密码：走 confirm 分支即可，权限判定应在其之前/之后都能拦住。
	rec := deletionRequest(t, h, userID, "")
	if rec.Code != http.StatusForbidden {
		t.Fatalf("租户所有者注销应 403，得到 %d body=%s", rec.Code, rec.Body.String())
	}
	var status string
	if err := pool.QueryRow(context.Background(),
		`SELECT account_status FROM users WHERE id = $1`, userID).Scan(&status); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if status != accountStatusActive {
		t.Fatalf("被拒绝后状态不该变：%q", status)
	}
}

// 重复申请必须幂等：不重复冻结、**不重置到期时间**。
//
// 重置到期时间会让用户无限延长"数据即将被删"的状态，那对双方都没有好处，
// 也会让到期清理任务永远扫不到它。
func TestLiveDeletionRequestIsIdempotent(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)
	setUserPassword(t, userID, "correct-password")
	h := mfaTestHandler(t)

	if rec := deletionRequest(t, h, userID, "correct-password"); rec.Code != http.StatusOK {
		t.Fatalf("首次申请失败: %s", rec.Body.String())
	}
	var firstDue time.Time
	if err := pool.QueryRow(context.Background(),
		`SELECT deletion_due_at FROM users WHERE id = $1`, userID).Scan(&firstDue); err != nil {
		t.Fatalf("读回到期时间失败: %v", err)
	}

	// 隔一小会儿再申请一次。
	time.Sleep(20 * time.Millisecond)
	if rec := deletionRequest(t, h, userID, "correct-password"); rec.Code != http.StatusOK {
		t.Fatalf("重复申请应幂等返回 200，得到 %d body=%s", rec.Code, rec.Body.String())
	}
	var secondDue time.Time
	if err := pool.QueryRow(context.Background(),
		`SELECT deletion_due_at FROM users WHERE id = $1`, userID).Scan(&secondDue); err != nil {
		t.Fatalf("读回到期时间失败: %v", err)
	}
	if !secondDue.Equal(firstDue) {
		t.Fatalf("重复申请重置了到期时间：%v → %v", firstDue, secondDue)
	}
}
