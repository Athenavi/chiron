package api

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/athenavi/chiron/internal/db"
)

// 真实 PostgreSQL 用例：**模型发现不得动用户设的 `enabled`**。
//
// 用户报（2026-10-09）：「模型偏好设置未持久化，导致 /models 页面刷新后全部变为启用」。
// 根因是 `replaceProviderModels` 旧实现「DELETE 该 provider 全部行 → 以 `enabled = true`
// 重新 INSERT」（且每次 id 都是新的 `gen_random_uuid()`）⇒ 管理员刚关掉的模型，下一次
// 模型发现就被重新启用；而 `GET /v1/models` 就会触发发现（对话页一打开就调）。
//
// 为什么非要有真库用例：纯函数 `planModelSync` 的单测只证明"**决策**对了"，
// 证明不了这条 SQL **真跑起来**的结果。而本 bug 的表现恰恰是"行被删掉重建"——
// 断言"被关掉的那一行还关着"在旧实现下会直接 `no rows`（那行已经不存在了）。
//
// 取值口径与既有 `*_live_test.go` 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置则 skip
// （CI 的 real-stack job 会注入）。
func withLiveModelPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置：跳过真实 PostgreSQL 用例")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("连接 PostgreSQL 失败: %v", err)
	}
	t.Cleanup(pool.Close)
	previous := db.Pool
	db.Pool = pool
	t.Cleanup(func() { db.Pool = previous })
	return pool
}

func TestLiveModelSyncKeepsUserEnabledFlag(t *testing.T) {
	pool := withLiveModelPool(t)
	ctx := context.Background()

	// 只碰这个测试专属 provider，跑完清干净（不与真实数据混在一起）
	const provider = "__test_model_sync_live__"
	cleanup := func() {
		if _, err := pool.Exec(context.Background(),
			`DELETE FROM llm_models WHERE provider = $1`, provider); err != nil {
			t.Logf("清理 %s 失败: %v", provider, err)
		}
	}
	cleanup()
	t.Cleanup(cleanup)

	// 三行起点：被启用的、**被关掉的**、以及稍后会"消失"的
	keptOn := uuid.NewString()
	keptOff := uuid.NewString()
	gone := uuid.NewString()
	seed := []struct {
		id      string
		name    string
		enabled bool
		window  int
	}{
		{keptOn, "kept-on-128k", true, 128000},
		// 名字带 `-128k` 后缀 ⇒ `inferContextWindow` 能推出 128000（用它来验证"未知窗口会被补"）。
		// ⚠ 别用无意义的名字（如 `kept-off`）：那种推不出窗口，`BackfillWin` 为空是**正确行为**，
		//    断言"补上了"会假红 —— 第一版就是栽在这。
		{keptOff, "kept-off-128k", false, 0},
		{gone, "gone-128k", true, 128000},
	}
	for _, s := range seed {
		if _, err := pool.Exec(ctx,
			`INSERT INTO llm_models (id, provider, name, display_name, enabled, context_window, created_at, updated_at)
			 VALUES ($1, $2, $3, $3, $4, $5, NOW(), NOW())`,
			s.id, provider, s.name, s.enabled, s.window); err != nil {
			t.Fatalf("建 %s 失败: %v", s.name, err)
		}
	}

	// 一次模型发现：既有两行 + 一个新模型；"gone" 不再出现
	if err := replaceProviderModels(ctx, provider, []string{"kept-on-128k", "kept-off-128k", "brand-new"}); err != nil {
		t.Fatalf("replaceProviderModels 失败: %v", err)
	}

	// ① 核心断言：被用户关掉的模型**必须还是关的**。
	//    旧实现下这一行会被 DELETE ⇒ 这里报 `no rows`（正是本用例要抓的回归）。
	var enabled bool
	if err := pool.QueryRow(ctx,
		`SELECT enabled FROM llm_models WHERE id = $1`, keptOff).Scan(&enabled); err != nil {
		t.Fatalf("被关掉的模型行不见了（说明它被删掉重建了，用户偏好没活过模型发现）: %v", err)
	}
	if enabled {
		t.Fatalf("被用户关掉的模型在这次模型发现后被重新启用了 ⇒ 偏好未持久化")
	}

	// ② 行 id 不变：前端 `PUT /v1/admin/models/{id}` 打的就是它
	var n int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM llm_models WHERE provider = $1 AND id = $2`, provider, keptOn).Scan(&n); err != nil || n != 1 {
		t.Fatalf("已存在行的 id 被换掉了（count=%d err=%v）⇒ 用户点开关会打到不存在的行", n, err)
	}
	// ③ 启用的那一行仍然是启用的
	var onEnabled bool
	if err := pool.QueryRow(ctx, `SELECT enabled FROM llm_models WHERE id = $1`, keptOn).Scan(&onEnabled); err != nil || !onEnabled {
		t.Fatalf("本应保持启用的行被关掉了（enabled=%v err=%v）", onEnabled, err)
	}

	// ④ 新发现的模型被建出来，且默认启用
	var newEnabled bool
	var newWindow int
	if err := pool.QueryRow(ctx,
		`SELECT enabled, context_window FROM llm_models WHERE provider = $1 AND name = 'brand-new'`,
		provider).Scan(&newEnabled, &newWindow); err != nil {
		t.Fatalf("新发现的模型没有被写入: %v", err)
	}
	if !newEnabled {
		t.Fatalf("新发现的模型应默认启用")
	}
	// ⑤ 窗口只在"之前不知道"时补：kept-off-128k 从 0 补成 128000；kept-on-128k 保持原值
	var offWindow int
	if err := pool.QueryRow(ctx,
		`SELECT context_window FROM llm_models WHERE id = $1`, keptOff).Scan(&offWindow); err != nil || offWindow != 128000 {
		t.Fatalf("未知窗口没有被补成 128000（window=%d err=%v）", offWindow, err)
	}
	var onWindow int
	if err := pool.QueryRow(ctx,
		`SELECT context_window FROM llm_models WHERE id = $1`, keptOn).Scan(&onWindow); err != nil || onWindow != 128000 {
		t.Fatalf("已知窗口被覆盖了（window=%d err=%v，应为 128000）", onWindow, err)
	}

	// ⑥ 这次没再被发现的行被删掉
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM llm_models WHERE id = $1`, gone).Scan(&n); err != nil || n != 0 {
		t.Fatalf("消失的模型没有被删除（count=%d err=%v）", n, err)
	}
}
