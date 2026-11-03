package api

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// `ensureFolderChain` 的真实 PostgreSQL 用例。
//
// 为什么必须是真实库：它依赖三件只有 PG 才能给出答案的东西 ——
//  1. `pg_advisory_xact_lock(hashtext(...))` 是否可用且不报错；
//  2. `COALESCE(parent_id, '')` 与 `gen_random_uuid()` 组合出的 SQL 是否合法；
//  3. **重复调用是否幂等**（同一路径第二次发布不能造出第二个同名文件夹）。
//
// 一个纯函数测试覆盖不到任何一条。而"文件夹重复/层级错乱"恰恰是用户在媒体库里
// 一眼就能看见、却很难从日志推断的问题。
//
// 取值口径与既有 `*_live_test.go` 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置则 skip。
func withLivePool(t *testing.T) *pgxpool.Pool {
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
	return pool
}

// liveMediaTenant 找一个可用的 tenant，并建一个唯一用户，用例结束清理。
//
// `media_assets.tenant_id` 有外键指向 `tenants(id)`、`user_id` 是 varchar：
// 直接编一个 tenant 会撞外键，所以取库里真实存在的那个。
func liveMediaFixture(t *testing.T, pool *pgxpool.Pool) (tenantID int16, userID string) {
	t.Helper()
	ctx := context.Background()

	if err := pool.QueryRow(ctx, `SELECT id FROM tenants ORDER BY id LIMIT 1`).Scan(&tenantID); err != nil {
		t.Skipf("库里没有 tenant 可用（%v）：跳过媒体层级用例", err)
	}
	userID = "live-media-" + time.Now().Format("20060102150405.000000")
	t.Cleanup(func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_, _ = pool.Exec(cleanup, `DELETE FROM media_assets WHERE tenant_id = $1 AND user_id = $2`, tenantID, userID)
	})
	return tenantID, userID
}

func TestEnsureFolderChainLive(t *testing.T) {
	pool := withLivePool(t)
	tenantID, userID := liveMediaFixture(t, pool)
	ctx := context.Background()

	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	parentID, err := ensureFolderChain(ctx, tx, tenantID, userID, "", []string{"reports", "2024"})
	if err != nil {
		t.Fatalf("ensureFolderChain: %v", err)
	}
	if parentID == "" {
		t.Fatal("末级文件夹 id 不应为空")
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatalf("commit: %v", err)
	}

	// 断言 1：两级文件夹都落库，且层级正确（不是两个都在根下）。
	var reportsID string
	if err := pool.QueryRow(ctx,
		`SELECT id FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND type='folder'
		    AND COALESCE(parent_id,'')='' AND name='reports'`, tenantID, userID).Scan(&reportsID); err != nil {
		t.Fatalf("根下的 reports 文件夹不存在: %v", err)
	}
	var nestedParent string
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(parent_id,'') FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND type='folder' AND name='2024'`, tenantID, userID).Scan(&nestedParent); err != nil {
		t.Fatalf("2024 文件夹不存在: %v", err)
	}
	if nestedParent != reportsID {
		t.Errorf("2024 的 parent_id = %q, 期望 %q（应挂在 reports 下，而不是根目录）", nestedParent, reportsID)
	}

	// 断言 2：重复调用必须**复用**已有文件夹，不能再造一条。
	// 这条最关键 —— 缺了它，用户每发布一次同一路径，媒体库里就多一层同名目录。
	tx2, err := pool.Begin(ctx)
	if err != nil {
		t.Fatalf("begin #2: %v", err)
	}
	defer func() { _ = tx2.Rollback(ctx) }()
	againID, err := ensureFolderChain(ctx, tx2, tenantID, userID, "", []string{"reports", "2024"})
	if err != nil {
		t.Fatalf("ensureFolderChain #2: %v", err)
	}
	if err := tx2.Commit(ctx); err != nil {
		t.Fatalf("commit #2: %v", err)
	}
	if againID != parentID {
		t.Errorf("重复调用返回了新的文件夹 id（%q → %q）：幂等性被破坏", parentID, againID)
	}

	var folderCount int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND type='folder'`, tenantID, userID).Scan(&folderCount); err != nil {
		t.Fatalf("count folders: %v", err)
	}
	if folderCount != 2 {
		t.Errorf("文件夹总数 = %d, 期望 2（reports + 2024）；重复调用造出了重复目录", folderCount)
	}

	// 断言 3：空链（根目录）返回空串，不建任何文件夹。
	tx3, err := pool.Begin(ctx)
	if err != nil {
		t.Fatalf("begin #3: %v", err)
	}
	defer func() { _ = tx3.Rollback(ctx) }()
	rootID, err := ensureFolderChain(ctx, tx3, tenantID, userID, "", nil)
	if err != nil {
		t.Fatalf("ensureFolderChain(空链): %v", err)
	}
	if rootID != "" {
		t.Errorf("空链应返回空串（表示根目录），got %q", rootID)
	}
}

// 长路径落库的端到端断言：解析 → 建链 → 资产挂在末级文件夹下。
func TestLongPathAssetLandsInNestedFoldersLive(t *testing.T) {
	pool := withLivePool(t)
	tenantID, userID := liveMediaFixture(t, pool)
	ctx := context.Background()

	folders, name, err := splitAssetPath("reports/2024/q3.md")
	if err != nil {
		t.Fatalf("splitAssetPath: %v", err)
	}
	if name != "q3.md" {
		t.Fatalf("name = %q", name)
	}

	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	parentID, err := ensureFolderChain(ctx, tx, tenantID, userID, "", folders)
	if err != nil {
		t.Fatalf("ensureFolderChain: %v", err)
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, size, parent_id, created_at, updated_at)
		 VALUES (gen_random_uuid(), $1, $2, 'text', $3, '', 4, $4, NOW(), NOW())`,
		tenantID, userID, name, parentID); err != nil {
		t.Fatalf("插入资产: %v", err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatalf("commit: %v", err)
	}

	// 资产必须挂在 2024 下，而不是根目录 —— 这正是"层级被压平"的判定点。
	var assetParent string
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(parent_id,'') FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND name=$3`, tenantID, userID, name).Scan(&assetParent); err != nil {
		t.Fatalf("资产未落库: %v", err)
	}
	if assetParent != parentID {
		t.Errorf("资产 parent_id = %q, 期望 %q（应嵌在 reports/2024 下）", assetParent, parentID)
	}

	// 根目录列表**不应**包含它（它不在根下）。
	var rootHas int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND COALESCE(parent_id,'')='' AND name=$3`,
		tenantID, userID, name).Scan(&rootHas); err != nil {
		t.Fatalf("count root: %v", err)
	}
	if rootHas != 0 {
		t.Errorf("资产出现在了根目录（%d 条），层级被压平", rootHas)
	}
}
