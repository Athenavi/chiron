package api

import (
	"strings"
	"sync/atomic"
)

// ── CORS 白名单运行时共享源──
//
// 背景：CORSMiddleware / checkWebSocketOrigin / billing.firstOrigin 之前各自读取
// 构造期快照（cfg.CORSOrigins 或 os.Getenv("CORS_ORIGINS")），后台「系统设置」保存
// cors 分类后其它读取点与其它副本不会生效。
//
// 本文件提供进程内唯一共享源：启动时由 gateway_router 注入 cfg.CORSOrigins，
// 后台保存 cors 后本实例与跨副本订阅者调用 SetCORSAllowOrigin，三处读取点即时一致。

var corsAllowOrigin atomic.Value // string：逗号分隔白名单（空 = 未配置，拒绝带 Origin 的浏览器请求）

// SetCORSAllowOrigin 更新运行时 CORS 白名单。
func SetCORSAllowOrigin(origins string) {
	corsAllowOrigin.Store(origins)
}

// currentCORSAllowOrigin 读取当前白名单（未设置时为空串）。
func currentCORSAllowOrigin() string {
	v := corsAllowOrigin.Load()
	if v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}

// corsOriginAllowed 判定 Origin 是否在白名单内。
// 语义与历史一致："*" 在 AllowCredentials=true 下违反 CORS 规范且高危，显式拒绝；
// 白名单未配置时拒绝所有带 Origin 的跨域请求（非浏览器客户端无 Origin 不受影响）。
func corsOriginAllowed(origin string) bool {
	if origin == "" {
		return true
	}
	allow := currentCORSAllowOrigin()
	if allow == "" || allow == "*" {
		return false
	}
	for _, o := range strings.Split(allow, ",") {
		if strings.TrimSpace(o) == origin {
			return true
		}
	}
	return false
}
