package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/session"
)

// postBranch 发一次分支请求。claims 为 nil 时模拟未登录。
//
// 用零值 `&session.Manager{}`：它没有 DB（pool 为 nil），因此**参数校验之后的**路径会
// 稳定返回 "database unavailable" —— 正好用来区分"被参数校验挡住"与"过了校验"。
func postBranch(claims *auth.Claims, sessionID, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, "/v1/conversations/x/branch", strings.NewReader(body))
	req.SetPathValue("id", sessionID)
	if claims != nil {
		req = req.WithContext(auth.WithClaims(req.Context(), claims))
	}
	rec := httptest.NewRecorder()
	BranchConversationHandler(&session.Manager{}, nil).ServeHTTP(rec, req)
	return rec
}

// TestBranchHandlerParameterValidation 参数校验是分支端点的第一道闸门：
// 越界/非法的参数必须**在碰数据库之前**被拒，且错误信息要能指导调用方改对。
func TestBranchHandlerParameterValidation(t *testing.T) {
	claims := &auth.Claims{TenantID: "t1", UserID: "u1"}

	t.Run("未登录 401", func(t *testing.T) {
		if got := postBranch(nil, "s1", `{"from_index":5}`).Code; got != http.StatusUnauthorized {
			t.Errorf("code = %d, want 401", got)
		}
	})

	t.Run("缺少会话 id 400", func(t *testing.T) {
		if got := postBranch(claims, "", `{"from_index":5}`).Code; got != http.StatusBadRequest {
			t.Errorf("code = %d, want 400", got)
		}
	})

	t.Run("from_index 必须为正 400", func(t *testing.T) {
		rec := postBranch(claims, "s1", `{"from_index":0}`)
		if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "from_index") {
			t.Errorf("code = %d body = %s", rec.Code, rec.Body.String())
		}
	})

	t.Run("mode 只接受 condense/truncate 400", func(t *testing.T) {
		rec := postBranch(claims, "s1", `{"from_index":5,"mode":"compact"}`)
		if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "mode must be") {
			t.Errorf("code = %d body = %s", rec.Code, rec.Body.String())
		}
	})

	t.Run("keep_tail 不能为负 400", func(t *testing.T) {
		rec := postBranch(claims, "s1", `{"from_index":5,"keep_tail":-1}`)
		if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "keep_tail") {
			t.Errorf("code = %d body = %s", rec.Code, rec.Body.String())
		}
	})

	t.Run("请求体不是 JSON 400", func(t *testing.T) {
		if got := postBranch(claims, "s1", "not-json").Code; got != http.StatusBadRequest {
			t.Errorf("code = %d, want 400", got)
		}
	})

	t.Run("合法参数会走到存储层（此处无 DB，于是报 database unavailable）", func(t *testing.T) {
		rec := postBranch(claims, "s1", `{"from_index":12,"keep_tail":4,"mode":"condense"}`)
		if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "database unavailable") {
			t.Errorf("code = %d body = %s（说明参数校验放行了，但没走到存储层）", rec.Code, rec.Body.String())
		}
	})
}
