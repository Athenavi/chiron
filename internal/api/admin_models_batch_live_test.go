package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/google/uuid"
)

// 真实 PostgreSQL 用例：批量开关必须**真的**落到行上，且计数不多不少。
//
// 为什么非要有真库用例：这条 SQL 有两处只有真库才暴露的坑 ——
//   ① 参数是 []string。写成 `$2::uuid[]` 会被 Postgres 直接拒绝（text[] → uuid[]
//      没有这个 cast），必须 unnest 之后逐行 `::uuid`；
//   ② 返回的 `updated` 是前端提示"已更新 N 个"的唯一来源，而前端两级分组下
//      "跨组全选"必然把同一个 id 送两次。
// 纯函数单测只能证明 id 归一化对了，证明不了这条 UPDATE 跑得通、数得对。
//
// 取值口径与既有 *_live_test.go 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置则 skip。
func TestLiveBatchUpdateModels(t *testing.T) {
	pool := withLiveModelPool(t)
	ctx := context.Background()

	// 只碰这个测试专属 provider，跑完清干净（不与真实数据混在一起）
	const provider = "__test_batch_update_live__"
	cleanup := func() {
		if _, err := pool.Exec(context.Background(),
			`DELETE FROM llm_models WHERE provider = $1`, provider); err != nil {
			t.Logf("清理 %s 失败: %v", provider, err)
		}
	}
	cleanup()
	t.Cleanup(cleanup)

	// 三行：两行先启用、一行先禁用 —— 两个方向都要覆盖，且"禁用"才是最常用的那个。
	ids := make([]string, 3)
	for i := range ids {
		ids[i] = uuid.NewString()
		if _, err := pool.Exec(ctx,
			`INSERT INTO llm_models (id, provider, name, display_name, enabled, context_window)
			 VALUES ($1, $2, $3, $4, $5, 128000)`,
			ids[i], provider, fmt.Sprintf("batch-live-%d", i), "batch live", i != 2); err != nil {
			t.Fatalf("插入测试行失败: %v", err)
		}
	}

	h := &AdminHandler{}
	missing := uuid.NewString() // 模拟"选择期间该行已被模型发现删掉"的 id

	// 第一个 id 故意重复送一次（跨组全选就会这样），外加一个不存在的 id：
	// updated 必须是 3 —— 不是 4（重复未去重），也不是 4（把不存在的算进去）。
	body := fmt.Sprintf(`{"ids":["%s","%s","%s","%s","%s"],"enabled":false}`,
		ids[0], ids[1], ids[0], ids[2], missing)
	w := httptest.NewRecorder()
	h.BatchUpdateModels(w, httptest.NewRequest(http.MethodPut, "/models/batch", strings.NewReader(body)))
	if w.Code != http.StatusOK {
		t.Fatalf("期望 200，实得 %d：%s", w.Code, w.Body.String())
	}
	var resp struct {
		Success bool `json:"success"`
		Data    struct {
			Updated int  `json:"updated"`
			Enabled bool `json:"enabled"`
		} `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应不是预期 JSON：%v（%s）", err, w.Body.String())
	}
	if !resp.Success || resp.Data.Enabled {
		t.Fatalf("响应形状不对：%s", w.Body.String())
	}
	if resp.Data.Updated != 3 {
		t.Fatalf("updated 应为 3（去重、且不含不存在的 id），实得 %d", resp.Data.Updated)
	}

	// 真落库：三行全部关掉
	var on int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM llm_models WHERE provider = $1 AND enabled = true`, provider).Scan(&on); err != nil {
		t.Fatalf("回查失败: %v", err)
	}
	if on != 0 {
		t.Fatalf("批量禁用后不该还有启用的行，实得 %d 行仍启用", on)
	}

	// 反向再走一次：批量启用 → 三行都开（证明 enabled=true 与 false 都真的生效）
	body = fmt.Sprintf(`{"ids":["%s","%s","%s"],"enabled":true}`, ids[0], ids[1], ids[2])
	w = httptest.NewRecorder()
	h.BatchUpdateModels(w, httptest.NewRequest(http.MethodPut, "/models/batch", strings.NewReader(body)))
	if w.Code != http.StatusOK {
		t.Fatalf("批量启用期望 200，实得 %d：%s", w.Code, w.Body.String())
	}
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM llm_models WHERE provider = $1 AND enabled = true`, provider).Scan(&on); err != nil {
		t.Fatalf("回查失败: %v", err)
	}
	if on != 3 {
		t.Fatalf("批量启用后三行都该启用，实得 %d", on)
	}
}
