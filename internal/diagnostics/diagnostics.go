// Package diagnostics 把"**配了但不生效**"变成可自答的问题清单。
//
// 背景：本仓库反复出现过同一类故障 —— 配置写了、界面也显示"已保存"，但功能不生效。
// 典型形态有三种：
//
//  1. **值不合法**（长度不够 / 格式不对），被启动校验或运行期 fail-closed 拒绝；
//  2. **值合法但不是这个部署需要的**（例如 `DEGRADED_MODE=true` 让多副本各跑各的）；
//  3. **依赖不可达**（Redis/PG/引擎/沙箱），配置本身没错，但链路断了。
//
// 排障时这三种都表现为"我配了，但没用"，而日志里各自出现在不同位置。本包把它们收成一份
// **可读的诊断报告**：每条检查给出状态 + 事实（detail）+ 该怎么办（remediation）。
//
// 设计纪律：
//
//   - **判定是纯函数**（`Summarize` / `Build` / `CheckConfig`），因此可单测；真正"探活"的
//     I/O 在调用方（internal/api 的诊断处理器）完成，把观测到的事实传进来。
//   - **不确定就 warn，绝不假 ok**：拿不到事实时用 `StatusSkipped`（明确"没检查"），
//     而不是 `StatusOK`（那会让人以为已验证）。
package diagnostics

import "sort"

// Status 是一条检查的结论。
type Status string

const (
	// StatusOK：已确认正常。
	StatusOK Status = "ok"
	// StatusWarn：能用，但存在会咬人的隐患（例如缺少信任代理网段）。
	StatusWarn Status = "warn"
	// StatusFail：不可用 / 配置必然不生效，需要处理。
	StatusFail Status = "fail"
	// StatusSkipped：**没有被检查**（缺前置条件）。它不是"通过" —— 见包注释的纪律。
	StatusSkipped Status = "skipped"
)

// Check 是一条诊断项。
type Check struct {
	// ID 稳定标识（前端据此定位、文档据此引用）。
	ID string `json:"id"`
	// Title 一句话说明这条检查在查什么。
	Title string `json:"title"`
	// Category 分组（config / dependency / capability），前端据此分栏。
	Category string `json:"category"`
	Status   Status `json:"status"`
	// Detail 是 host **观测到的事实**（不是建议）。
	Detail string `json:"detail"`
	// Remediation 是"该怎么办"；ok 时为空。
	Remediation string `json:"remediation,omitempty"`
}

// 类别常量。
const (
	CategoryConfig     = "config"
	CategoryDependency = "dependency"
	CategoryCapability = "capability"
)

// Report 是一次诊断的完整结果。
type Report struct {
	GeneratedAt int64          `json:"generated_at"`
	Status      Status         `json:"status"`
	Counts      map[string]int `json:"counts"`
	Checks      []Check        `json:"checks"`
}

// Summarize 汇总多条检查的总体状态（**纯函数**）。
//
// 优先级：任一 fail ⇒ fail；否则任一 warn ⇒ warn；否则若**全部** skipped ⇒ skipped；
// 否则 ok。最后一条很重要：全 skipped 时不能报 ok，否则"什么都没查"会被读成"一切正常"。
func Summarize(checks []Check) Status {
	if len(checks) == 0 {
		return StatusSkipped
	}
	allSkipped := true
	anyWarn := false
	for _, c := range checks {
		switch c.Status {
		case StatusFail:
			return StatusFail
		case StatusWarn:
			anyWarn = true
			allSkipped = false
		case StatusOK:
			allSkipped = false
		case StatusSkipped:
			// 保持 allSkipped 的候选资格
		}
	}
	if allSkipped {
		return StatusSkipped
	}
	if anyWarn {
		return StatusWarn
	}
	return StatusOK
}

// Build 组装报告（**纯函数**：给定同样的 checks 与时间戳，结果相同）。
// 检查按类别、再按 ID 稳定排序，便于前端与文档对齐。
func Build(checks []Check, generatedAt int64) Report {
	ordered := make([]Check, len(checks))
	copy(ordered, checks)
	sort.SliceStable(ordered, func(i, j int) bool {
		if ordered[i].Category != ordered[j].Category {
			return categoryRank(ordered[i].Category) < categoryRank(ordered[j].Category)
		}
		return ordered[i].ID < ordered[j].ID
	})

	counts := map[string]int{
		string(StatusOK): 0, string(StatusWarn): 0,
		string(StatusFail): 0, string(StatusSkipped): 0,
	}
	for _, c := range ordered {
		counts[string(c.Status)]++
	}
	return Report{
		GeneratedAt: generatedAt,
		Status:      Summarize(ordered),
		Counts:      counts,
		Checks:      ordered,
	}
}

func categoryRank(c string) int {
	switch c {
	case CategoryDependency:
		return 0
	case CategoryConfig:
		return 1
	default:
		return 2
	}
}

// ── 配置卫生检查（纯函数） ──────────────────────────────────────────────────

// ConfigFacts 是诊断需要的配置事实（由调用方从 config.Config 提取）。
//
// **刻意用"事实字段"而不是直接传 *config.Config**：这样本函数是纯的、可单测，
// 也逼着调用方把"到底读了哪些字段"写清楚（少一个字段 = 少一条检查，看得见）。
type ConfigFacts struct {
	AppSecretSet  bool
	AppSecretLen  int
	JWTSecretSet  bool
	InternalSet   bool
	DegradedMode  bool
	TrustedProxy  []string
	StorageBackend string
	S3Endpoint    string
	S3Bucket      string
	S3AccessKey   bool
	S3SecretKey   bool
	CORSOrigins   string
	MetricsToken  bool
	PublicBaseURL string
	CookieSecure  bool
	// RedisMode 取值 single / sentinel / cluster（来自部署配置）。
	RedisMode string
	// SandboxBackend 是**引擎**的沙箱模式（service / local）。
	SandboxBackend string
	SandboxURL     string
}

// CheckConfig 对配置事实做卫生检查（**纯函数**）。
//
// 每条都对应一个真实踩过的坑，注释里写清"为什么会咬人"——否则这些检查会被当成形式主义删掉。
func CheckConfig(f ConfigFacts) []Check {
	var out []Check
	add := func(id, title, cat string, st Status, detail, fix string) {
		out = append(out, Check{ID: id, Title: title, Category: cat, Status: st, Detail: detail, Remediation: fix})
	}

	// APP_SECRET 是唯一根密钥：派生 JWT/INTERNAL，并加密 system_settings 里的敏感项。
	// 过短 ⇒ 启动校验拒绝；更换 ⇒ 后台配置无法解密（且**不可恢复**）。
	switch {
	case !f.AppSecretSet:
		add("config.app_secret", "APP_SECRET 已设置", CategoryConfig, StatusFail,
			"APP_SECRET 未设置。它是唯一根密钥（派生 JWT_SECRET / INTERNAL_TOKEN 并加密后台配置），缺失时无法启动。",
			"设置一个 ≥32 字符的随机串；多副本必须完全一致。")
	case f.AppSecretLen < 32:
		add("config.app_secret", "APP_SECRET 已设置", CategoryConfig, StatusFail,
			"APP_SECRET 长度不足 32（当前长度见 detail 证据），启动校验会拒绝。",
			"换一个 ≥32 字符的随机串。注意：更换会导致已加密的后台配置无法解密。")
	default:
		add("config.app_secret", "APP_SECRET 已设置", CategoryConfig, StatusOK,
			"已设置且长度达标。", "")
	}

	if !f.JWTSecretSet || !f.InternalSet {
		add("config.derived_secrets", "派生密钥（JWT / INTERNAL）", CategoryConfig, StatusOK,
			"未显式设置时会从 APP_SECRET 派生 —— 这是预期行为，不是问题。", "")
	} else {
		add("config.derived_secrets", "派生密钥（JWT / INTERNAL）", CategoryConfig, StatusOK,
			"已显式设置：多副本必须完全一致，否则跨副本签发/校验会失败。", "")
	}

	// DEGRADED_MODE：单机开发用；多副本下会让各副本看到不同的会话/限流/队列。
	if f.DegradedMode {
		add("config.degraded_mode", "DEGRADED_MODE", CategoryConfig, StatusWarn,
			"DEGRADED_MODE=true：Redis 不可用时不再拒绝启动，改为进程内降级。",
			"仅单机开发使用。多副本部署请置回 false —— 否则各副本的会话/限流/事件不一致。")
	} else {
		add("config.degraded_mode", "DEGRADED_MODE", CategoryConfig, StatusOK,
			"未开启：Redis 等依赖不可用时会拒绝启动（fail-closed）。", "")
	}

	if len(f.TrustedProxy) == 0 {
		add("config.trusted_proxy", "TRUSTED_PROXY_CIDRS", CategoryConfig, StatusWarn,
			"未配置受信代理网段。",
			"经反向代理/LB 部署时必填：否则 IP 限流可被伪造（X-Forwarded-For 被采信），且 SSO 公开列表会 fail-closed 返回空。")
	} else {
		add("config.trusted_proxy", "TRUSTED_PROXY_CIDRS", CategoryConfig, StatusOK,
			"已配置受信代理网段，X-Forwarded-For 仅在来自这些网段时被采信。", "")
	}

	// 存储：cloud S3 需要端点/桶/凭据齐全；local 不需要。
	if f.StorageBackend == "s3" {
		var missing []string
		if f.S3Endpoint == "" {
			missing = append(missing, "S3_ENDPOINT")
		}
		if f.S3Bucket == "" {
			missing = append(missing, "S3_BUCKET")
		}
		if !f.S3AccessKey {
			missing = append(missing, "S3_ACCESS_KEY")
		}
		if !f.S3SecretKey {
			missing = append(missing, "S3_SECRET_KEY")
		}
		if len(missing) > 0 {
			add("config.storage_s3", "对象存储（S3）", CategoryConfig, StatusFail,
				"STORAGE_BACKEND=s3，但缺少："+join(missing)+"。",
				"补齐这些变量（云厂商 S3 端点填**纯 host**，不带 scheme）。缺任一项都会在真正上传时失败。")
		} else {
			add("config.storage_s3", "对象存储（S3）", CategoryConfig, StatusOK,
				"S3 端点/桶/凭据齐全。", "")
		}
	} else {
		add("config.storage_s3", "对象存储（S3）", CategoryConfig, StatusOK,
			"STORAGE_BACKEND="+orDash(f.StorageBackend)+"（非 S3，使用本地存储）。", "")
	}

	// 沙箱：引擎默认 service 且 fail-closed，端点配错 ⇒ 引擎拒绝启动。
	if f.SandboxBackend == "service" {
		if f.SandboxURL == "" {
			add("config.sandbox", "执行沙箱（引擎侧）", CategoryConfig, StatusFail,
				"SANDBOX_BACKEND=service 但 SANDBOX_SERVICE_URL 为空。",
				"设置沙箱服务地址（如 http://sandbox-service:8001）。引擎默认 fail-closed：探测失败会拒绝启动。")
		} else {
			add("config.sandbox", "执行沙箱（引擎侧）", CategoryConfig, StatusOK,
				"SANDBOX_BACKEND=service，已配置服务地址。", "")
		}
	} else {
		add("config.sandbox", "执行沙箱（引擎侧）", CategoryConfig, StatusWarn,
			"SANDBOX_BACKEND="+orDash(f.SandboxBackend)+"：未使用服务化沙箱。",
			"local 模式会牺牲执行隔离。生产建议 backend=service。")
	}

	// CORS：留空在生产会导致"前端调不通"，但开发常用。
	if f.CORSOrigins == "" {
		add("config.cors", "CORS_ORIGINS", CategoryConfig, StatusWarn,
			"未配置跨域白名单。",
			"同源部署（前端反代网关）可忽略；前后端分域部署必须填前端来源。")
	} else {
		add("config.cors", "CORS_ORIGINS", CategoryConfig, StatusOK, "已配置跨域白名单。", "")
	}

	// Cookie Secure 与部署协议必须一致，否则登录态挂了却看不出原因。
	if f.PublicBaseURL != "" && !f.CookieSecure {
		add("config.cookie_secure", "Cookie Secure 与部署协议", CategoryConfig, StatusWarn,
			"PUBLIC_BASE_URL 是 https，但 CookieSecure 未生效。",
			"检查 PUBLIC_BASE_URL 协议是否被正确推导；https 下 Cookie 不 Secure 会被浏览器拒收。")
	} else {
		add("config.cookie_secure", "Cookie Secure 与部署协议", CategoryConfig, StatusOK,
			"与部署协议一致。", "")
	}

	// 多副本 Redis 形态（单实例在多副本下是可用性单点，但不算"配置错误"，只提示）。
	if f.RedisMode == "" || f.RedisMode == "single" {
		add("config.redis_mode", "Redis 形态", CategoryConfig, StatusWarn,
			"Redis 为单实例（single）。",
			"多副本生产建议 sentinel 或 cluster：Redis 不可用会触发依赖门禁（拒绝启动/摘流量）。")
	} else {
		add("config.redis_mode", "Redis 形态", CategoryConfig, StatusOK,
			"Redis 形态："+f.RedisMode+"。", "")
	}

	return out
}

// ── 依赖检查的构造器（事实由调用方探测后传入） ────────────────────────────────

// Dep 描述一次依赖探活的结果。
type Dep struct {
	ID      string
	Title   string
	Reached bool
	Detail  string
	// Fix 是探测失败时的补救说明。
	Fix string
}

// CheckDependencies 把依赖探活结果转成检查项（**纯函数**）。
func CheckDependencies(deps []Dep) []Check {
	var out []Check
	for _, d := range deps {
		st := StatusOK
		fix := ""
		if !d.Reached {
			st = StatusFail
			fix = d.Fix
		}
		out = append(out, Check{
			ID: d.ID, Title: d.Title, Category: CategoryDependency,
			Status: st, Detail: d.Detail, Remediation: fix,
		})
	}
	return out
}

// Capability 描述一次"能力可用性"的判定（例如"有没有可用的模型"）。
type Capability struct {
	ID      string
	Title   string
	Status  Status
	Detail  string
	Fix     string
}

// CheckCapabilities 把能力判定转成检查项（**纯函数**）。
func CheckCapabilities(caps []Capability) []Check {
	var out []Check
	for _, c := range caps {
		out = append(out, Check{
			ID: c.ID, Title: c.Title, Category: CategoryCapability,
			Status: c.Status, Detail: c.Detail, Remediation: c.Fix,
		})
	}
	return out
}

func orDash(s string) string {
	if s == "" {
		return "(未设置)"
	}
	return s
}

func join(items []string) string {
	out := ""
	for i, s := range items {
		if i > 0 {
			out += ", "
		}
		out += s
	}
	return out
}
