package billing

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/id"
)

// 套餐目录后台管理（P2-2c）的真库测试。
//
// 单独成文件而不是塞进 entitlements_live_test.go：那边测的是"购买 → 发权益"的
// 用户侧链路，这里测的是运营侧对目录的增改下架，两者的失败模式完全不同。
//
// 注意：真库已被 0010 种下 starter/standard/pro 三行，所以**不能**对总行数做断言，
// 只能断言"某个 code 在/不在结果里"。
func TestLiveAdminPackageCRUD(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	pool := setupLivePool(t, ctx, dsn)
	store := NewPGStore()

	// ux_billing_packages_code 是全站唯一索引，上次失败运行可能残留同 code 的行。
	suffix := id.NextID()
	code := "test_admin_pkg_" + suffix
	otherCode := "test_admin_pkg_other_" + suffix

	t.Cleanup(func() {
		bg := context.Background()
		for _, q := range []string{
			`DELETE FROM billing_packages WHERE code = $1`,
		} {
			for _, c := range []string{code, otherCode} {
				if _, e := pool.Exec(bg, q, c); e != nil {
					t.Logf("清理失败（%s, %s）: %v", q, c, e)
				}
			}
		}
	})

	// ── CreatePackage：主键在 store 内生成，调用方不该自己拼 ──
	pkg := &Package{
		Code:          code,
		Name:          "后台建包",
		Credits:       1500,
		PriceCents:    1800,
		Currency:      "CNY",
		DurationDays:  45,
		AllowedModels: []string{"gpt-4o-mini"},
		Enabled:       true,
		SortOrder:     77,
	}
	if err := store.CreatePackage(ctx, pkg); err != nil {
		t.Fatalf("CreatePackage: %v", err)
	}
	if pkg.ID == "" {
		t.Fatal("CreatePackage 后 pkg.ID 仍为空 —— 调用方无法拿到主键去更新/下架")
	}

	// 落库内容必须与入参一致（allowed_models 是 text[]，容易在扫描/写入时出错）。
	got, err := store.PackageByCode(ctx, code)
	if err != nil {
		t.Fatalf("PackageByCode: %v", err)
	}
	if got == nil {
		t.Fatal("建包后 PackageByCode 查不到")
	}
	if got.DurationDays != 45 || got.PriceCents != 1800 || got.Credits != 1500 {
		t.Errorf("落库字段不符: %+v", got)
	}
	if len(got.AllowedModels) != 1 || got.AllowedModels[0] != "gpt-4o-mini" {
		t.Errorf("allowed_models 不符: %v", got.AllowedModels)
	}

	// ── CreatePackage 撞码：必须返回可判定的哨兵错误，handler 才能区分 409/500 ──
	dup := &Package{Code: code, Name: "重复", Credits: 1, DurationDays: 1, Enabled: true}
	if err := store.CreatePackage(ctx, dup); !errors.Is(err, ErrPackageCodeExists) {
		t.Fatalf("重复 code 应返回 ErrPackageCodeExists，实际: %v", err)
	}

	// ── UpdatePackage：可编辑字段生效，code 不被改写 ──
	pkg.PriceCents = 2300
	pkg.Name = "后台改包"
	pkg.Credits = 2500
	found, err := store.UpdatePackage(ctx, pkg)
	if err != nil {
		t.Fatalf("UpdatePackage: %v", err)
	}
	if !found {
		t.Fatal("UpdatePackage 说 id 不存在，但它刚被创建")
	}
	got, err = store.PackageByCode(ctx, code)
	if err != nil || got == nil {
		t.Fatalf("更新后回查失败: %v", err)
	}
	if got.PriceCents != 2300 || got.Credits != 2500 || got.Name != "后台改包" {
		t.Errorf("更新未生效: %+v", got)
	}

	// ── UpdatePackage 不更新 code：传别的 code 应被**静默忽略** ──
	//
	// 这是刻意的设计（见 UpdatePackage 注释）：code 是 `payments.package_code`
	// 的引用值，改掉会让未确认的订单查不到套餐。所以这里断言的不是"撞唯一索引"，
	// 而是"code 纹丝不动" —— 传什么进去都不该改变它。
	other := &Package{Code: otherCode, Name: "另一个包", Credits: 100, DurationDays: 10, Enabled: true}
	if err := store.CreatePackage(ctx, other); err != nil {
		t.Fatalf("建第二个包: %v", err)
	}
	pkg.Code = otherCode // 假装调用方试图改 code
	if found, err := store.UpdatePackage(ctx, pkg); err != nil || !found {
		t.Fatalf("改 code 的更新不该失败: found=%v err=%v", found, err)
	}
	got, err = store.PackageByCode(ctx, code)
	if err != nil {
		t.Fatalf("回查: %v", err)
	}
	if got == nil {
		t.Fatal("原 code 查不到 —— code 被改掉了，会让未确认订单失去套餐引用")
	}
	// 也不能把另一个包的 code 抢过来
	if _, err := store.PackageByCode(ctx, otherCode); err != nil {
		t.Fatalf("回查第二个包: %v", err)
	}
	// 恢复成自己的 code，后续步骤继续用
	pkg.Code = code

	// ── SetPackageEnabled：下架后用户侧不可见，但后台仍可见 ──
	ok, err := store.SetPackageEnabled(ctx, pkg.ID, false)
	if err != nil {
		t.Fatalf("SetPackageEnabled(false): %v", err)
	}
	if !ok {
		t.Fatal("下架刚创建的套餐却说 id 不存在")
	}

	sold, err := store.ListPackages(ctx)
	if err != nil {
		t.Fatalf("ListPackages: %v", err)
	}
	for _, p := range sold {
		if p.Code == code {
			t.Error("下架后用户侧 ListPackages 仍返回该套餐 —— 用户会买到已下架的包")
		}
	}

	all, err := store.AdminListPackages(ctx)
	if err != nil {
		t.Fatalf("AdminListPackages: %v", err)
	}
	var seen bool
	for _, p := range all {
		if p.Code == code {
			seen = true
			if p.Enabled {
				t.Error("后台列表里该套餐 enabled 应为 false")
			}
		}
	}
	if !seen {
		t.Error("后台列表应包含已下架套餐，否则「下架后还在吗」无从确认")
	}

	// 恢复上架后用户侧应重新可见
	if ok, err := store.SetPackageEnabled(ctx, pkg.ID, true); err != nil || !ok {
		t.Fatalf("SetPackageEnabled(true): ok=%v err=%v", ok, err)
	}
	sold, err = store.ListPackages(ctx)
	if err != nil {
		t.Fatalf("ListPackages: %v", err)
	}
	var back bool
	for _, p := range sold {
		if p.Code == code {
			back = true
		}
	}
	if !back {
		t.Error("恢复上架后用户侧应能再看到该套餐")
	}

	// ── 不存在的 id：两个写操作都应返回 false 而不是 error ──
	missing, err := id.UUID()
	if err != nil {
		t.Fatalf("生成 UUID: %v", err)
	}
	if ok, err := store.SetPackageEnabled(ctx, missing, false); err != nil || ok {
		t.Errorf("不存在的 id 下架: ok=%v err=%v（期望 false, nil）", ok, err)
	}
	if found, err := store.UpdatePackage(ctx, &Package{ID: missing, Name: "x", DurationDays: 1, Enabled: true}); err != nil || found {
		t.Errorf("不存在的 id 更新: found=%v err=%v（期望 false, nil）", found, err)
	}
}
