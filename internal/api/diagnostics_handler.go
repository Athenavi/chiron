package api

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/diagnostics"
)

// diagnostics_handler.go —— "配了但不生效"的自诊断端点。
//
// GET /v1/admin/diagnostics
//
// 它做两件事，且**刻意分开**：
//
//  1. **探活**（IO）：PG / Redis / 引擎 / 模型 等真实连通性 —— 只有这里碰网络；
//  2. **判定**（纯函数）：`internal/diagnostics` 把这些观测与配置事实转成结论与补救建议。
//
// 分开是为了让"结论怎么来的"可单测（见 internal/diagnostics 的单测），而不是散落在一堆
// if 里。这个端点本身不改变任何状态。

// GetDiagnostics 返回诊断报告。
func (h *AdminHandler) GetDiagnostics(w http.ResponseWriter, r *http.Request) {
	// 探活要有上限：依赖挂了时不能让管理员页面一直转圈（每条 ping 最多几秒）。
	ctx, cancel := context.WithTimeout(r.Context(), 8*time.Second)
	defer cancel()

	checks := diagnostics.CheckDependencies(h.probeDependencies(ctx))
	checks = append(checks, diagnostics.CheckConfig(h.configFacts())...)
	checks = append(checks, diagnostics.CheckCapabilities(h.probeCapabilities(ctx))...)

	OK(w, diagnostics.Build(checks, time.Now().Unix()))
}

// probeDependencies 探测外部依赖（唯一做 IO 的地方）。
func (h *AdminHandler) probeDependencies(ctx context.Context) []diagnostics.Dep {
	deps := make([]diagnostics.Dep, 0, 3)

	// ── PostgreSQL ──
	pg := diagnostics.Dep{
		ID:    "dep.postgres",
		Title: "PostgreSQL",
		Fix: "检查 POSTGRES_DSN 与数据库可达性。注意：**应用不执行迁移** —— " +
			"需先用 `python -m alembic -c alembic.ini upgrade head` 把 schema 升到位，否则启动会因必需表缺失而拒绝。",
	}
	switch {
	case db.Pool == nil:
		pg.Detail = "连接池未初始化（进程可能尚未完成 DB 初始化）"
	default:
		if err := db.Pool.Ping(ctx); err != nil {
			pg.Detail = "ping 失败：" + truncErr(err, 200)
		} else {
			pg.Reached = true
			pg.Detail = "ping 成功"
			// schema 版本：应用**不执行迁移**，所以"版本对不对"是启动/查询失败最常见的原因之一，
			// 值得在这里明说。比对是只读的；读不到版本时不影响 ping 的结论。
			if _, actual, match, err := db.CheckSchemaVersion(ctx); err == nil {
				if match {
					pg.Detail += "；schema 版本 " + actual + "（与代码期望一致）"
				} else {
					pg.Reached = false
					pg.Detail = "ping 成功，但 schema 版本 " + actual + " 与代码期望的 head 不一致"
					pg.Fix = "执行 `python -m alembic -c alembic.ini upgrade head` 升级 schema —— " +
						"应用自身不执行迁移；版本不匹配时启动会因必需表/列缺失而失败。"
				}
			}
		}
	}
	deps = append(deps, pg)

	// ── Redis ──
	rd := diagnostics.Dep{
		ID:    "dep.redis",
		Title: "Redis",
		Fix: "检查 REDIS_URL。依赖门禁默认 fail-closed（DEGRADED_MODE=false 时 Redis 不可用会拒绝启动/摘流量）；" +
			"多副本另需所有副本 REDIS_KEY_PREFIX 一致。",
	}
	switch {
	case h.redis == nil:
		rd.Detail = "未配置"
	default:
		if err := h.redis.Ping(ctx).Err(); err != nil {
			rd.Detail = "ping 失败：" + truncErr(err, 200)
		} else {
			rd.Reached = true
			rd.Detail = "ping 成功"
		}
	}
	deps = append(deps, rd)

	// ── Python 引擎 ──
	en := diagnostics.Dep{
		ID:    "dep.engine",
		Title: "Python 引擎",
		Fix: "检查引擎进程与地址（静态清单 / 服务发现）。引擎未就绪时，提交会失败并留下 failed 的 turn。",
	}
	switch {
	case h.pythonClient == nil:
		en.Detail = "客户端未配置"
	case !h.pythonClient.IsConnected():
		en.Detail = "不可达（连接失败或未完成握手）"
	default:
		en.Reached = true
		en.Detail = "可达"
	}
	deps = append(deps, en)

	return deps
}

// configFacts 从配置里提取诊断需要的事实（**只读**）。
func (h *AdminHandler) configFacts() diagnostics.ConfigFacts {
	f := diagnostics.ConfigFacts{}
	if h.cfg == nil {
		return f
	}
	f.AppSecretSet = h.cfg.AppSecret != ""
	f.AppSecretLen = len(h.cfg.AppSecret)
	f.JWTSecretSet = h.cfg.JWTSecret != ""
	f.InternalSet = h.cfg.InternalToken != ""
	f.DegradedMode = h.cfg.DegradedMode
	f.TrustedProxy = h.cfg.TrustedProxyCIDRs
	f.StorageBackend = h.cfg.StorageBackend
	f.S3Endpoint = h.cfg.S3Endpoint
	f.S3Bucket = h.cfg.S3Bucket
	f.S3AccessKey = h.cfg.S3AccessKey != ""
	f.S3SecretKey = h.cfg.S3SecretKey != ""
	f.CORSOrigins = h.cfg.CORSOrigins
	f.MetricsToken = h.cfg.MetricsToken != ""
	f.PublicBaseURL = h.cfg.PublicBaseURL
	f.CookieSecure = h.cfg.CookieSecure
	f.RedisMode = redisModeOf(h.cfg)
	// 沙箱模式是**引擎**的配置，网关这里读不到 —— 如实标成 skipped，
	// 而不是猜一个值（猜错会把"没法确认"说成"配置有问题"）。
	f.SandboxBackend = ""
	f.SandboxURL = ""
	return f
}

// redisModeOf 从配置推导 Redis 形态。
//
// 网关只看得到 sentinel 地址配没配（cluster 形态由部署/引擎侧决定），所以这里**只区分
// sentinel 与 single** —— 拿不准的形态不猜，宁可少说一句也不误报。
func redisModeOf(cfg *config.Config) string {
	if cfg != nil && len(cfg.RedisSentinelAddrs) > 0 {
		return "sentinel"
	}
	return "single"
}

// probeCapabilities 探测"功能上能不能用"（例如有没有可用模型）。
func (h *AdminHandler) probeCapabilities(ctx context.Context) []diagnostics.Capability {
	caps := make([]diagnostics.Capability, 0, 1)

	models := diagnostics.Capability{
		ID:    "cap.models",
		Title: "可用模型",
		Fix:   "在「模型管理」里添加一个 provider 与 API Key（或到「API 密钥」配置），然后启用至少一个模型。",
	}
	switch {
	case db.Pool == nil:
		models.Status = diagnostics.StatusSkipped
		models.Detail = "数据库不可达，未能查询模型表"
	default:
		var total, enabled int
		err := db.Pool.QueryRow(ctx,
			`SELECT COUNT(*), COUNT(*) FILTER (WHERE enabled) FROM llm_models`).Scan(&total, &enabled)
		switch {
		case err != nil:
			models.Status = diagnostics.StatusSkipped
			models.Detail = "查询 llm_models 失败：" + truncErr(err, 200)
		case enabled == 0 && total == 0:
			models.Status = diagnostics.StatusFail
			models.Detail = "模型表为空 —— 对话无法进行"
		case enabled == 0:
			models.Status = diagnostics.StatusFail
			models.Detail = fmt.Sprintf("共 %d 个模型，但**没有一个启用**（界面里能看到却选不了）", total)
		default:
			models.Status = diagnostics.StatusOK
			models.Detail = fmt.Sprintf("已启用 %d / %d 个模型", enabled, total)
		}
	}
	caps = append(caps, models)
	return caps
}

func truncErr(err error, n int) string {
	s := strings.TrimSpace(err.Error())
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
