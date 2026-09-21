package api

import (
	"crypto/subtle"
	"log/slog"
	"net"
	"net/http"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/settings"
)

// internalTokenMW 校验 X-Internal-Token，供 Go 网关内部端点（引擎配置下发）使用。
// 缺失/不匹配时返回 401，绝不透出解密后的敏感配置。
// P0-3: 使用常量时间比较防止时序攻击
// 对于写操作端点（db/execute, db/batch-execute），额外限制仅允许本地回环地址调用，
// 防止 internal_token 泄露后任意 SQL 写入。
func internalTokenMW(cfg *config.Config, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		token := r.Header.Get("X-Internal-Token")
		if cfg.InternalToken == "" || token == "" || subtle.ConstantTimeCompare([]byte(cfg.InternalToken), []byte(token)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}

		// 对 DB 写操作端点额外限制仅回环地址可调用
		if r.URL.Path == "/v1/internal/db/execute" || r.URL.Path == "/v1/internal/db/batch-execute" {
			host, _, err := net.SplitHostPort(r.RemoteAddr)
			if err != nil {
				host = r.RemoteAddr
			}
			ip := net.ParseIP(host)
			if ip == nil || !(ip.IsLoopback() || ip.IsPrivate()) {
				slog.Warn("internal db write rejected from non-local address",
					"remote", r.RemoteAddr, "path", r.URL.Path)
				http.Error(w, "forbidden: db write only allowed from local/private network", http.StatusForbidden)
				return
			}
		}

		next(w, r)
	}
}

// EngineConfig GET /v1/internal/engine-config
// 供 Python AI 引擎启动时拉取「python」分类的后台配置（敏感键已由 APP_SECRET 解密）。
// 仅接受带 X-Internal-Token 的内部调用，防止配置外泄。
func EngineConfig(cfg *config.Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if db.Pool == nil {
			NotFound(w, "database unavailable")
			return
		}
		store := settings.New(db.Pool, cfg.AppSecret)
		m, err := store.LoadConfig(r.Context(), "python")
		if err != nil {
			slog.Error("engine config load failed", "error", err)
			InternalError(w, "failed to load engine config")
			return
		}
		// 服务提供商目录（权威源在网关侧）：引擎按目录注册 provider，
		// 避免引擎内硬编码 provider 列表与服务端漂移。
		m["llm_provider_catalog"] = llmProviderCatalogPayload()
		OK(w, m)
	}
}
