package auth

// ── OAuth2 / OIDC Provider 模板注册表 ─────────────────────
//
// 管理端选择 provider_type 后端点自动填充，仍可在数据库中覆盖
//（auth_url/token_url/userinfo_url 列非空即覆盖模板值）。

// ProviderProfile 是预设 provider 模板。
type ProviderProfile struct {
	Type        string   // google/github/wechat/dingtalk/feishu/qq/custom
	Protocol    string   // oidc | oauth2
	Issuer      string   // oidc 用
	AuthURL     string   // oauth2 用
	TokenURL    string   // oauth2 用
	UserinfoURL string   // oauth2 用
	Scopes      []string // 默认 scopes
}

// 协议常量。
const (
	ProtocolOIDC   = "oidc"
	ProtocolOAuth2 = "oauth2"
)

// Provider 类型常量。
const (
	ProviderGoogle   = "google"
	ProviderGitHub   = "github"
	ProviderWeChat   = "wechat"
	ProviderDingTalk = "dingtalk"
	ProviderFeishu   = "feishu"
	ProviderQQ       = "qq"
	ProviderCustom   = "custom"
)

// providerProfiles 内置模板（端点以各平台公开文档为准）。
var providerProfiles = map[string]ProviderProfile{
	ProviderGoogle: {
		Type:     ProviderGoogle,
		Protocol: ProtocolOIDC,
		Issuer:   "https://accounts.google.com",
		Scopes:   []string{"openid", "email", "profile"},
	},
	ProviderGitHub: {
		Type:        ProviderGitHub,
		Protocol:    ProtocolOAuth2,
		AuthURL:     "https://github.com/login/oauth/authorize",
		TokenURL:    "https://github.com/login/oauth/access_token",
		UserinfoURL: "https://api.github.com/user",
		Scopes:      []string{"read:user", "user:email"},
	},
	ProviderWeChat: {
		// 开放平台扫码登录；extra.mode=mp 时切换为公众号网页授权端点
		Type:        ProviderWeChat,
		Protocol:    ProtocolOAuth2,
		AuthURL:     "https://open.weixin.qq.com/connect/qrconnect",
		TokenURL:    "https://api.weixin.qq.com/sns/oauth2/access_token",
		UserinfoURL: "https://api.weixin.qq.com/sns/userinfo",
		Scopes:      []string{"snsapi_login"},
	},
	ProviderDingTalk: {
		Type:        ProviderDingTalk,
		Protocol:    ProtocolOAuth2,
		AuthURL:     "https://login.dingtalk.com/oauth2/auth",
		TokenURL:    "https://api.dingtalk.com/v1.0/oauth2/userAccessToken",
		UserinfoURL: "https://api.dingtalk.com/contact/users/me",
		Scopes:      []string{"openid"},
	},
	ProviderFeishu: {
		Type:        ProviderFeishu,
		Protocol:    ProtocolOAuth2,
		AuthURL:     "https://passport.feishu.cn/suite/passport/oauth/v2/authorize",
		TokenURL:    "https://passport.feishu.cn/suite/passport/oauth/v2/token",
		UserinfoURL: "https://passport.feishu.cn/suite/passport/oauth/v2/userinfo",
		Scopes:      []string{"user_info"},
	},
	ProviderQQ: {
		Type:        ProviderQQ,
		Protocol:    ProtocolOAuth2,
		AuthURL:     "https://graph.qq.com/oauth2.0/authorize",
		TokenURL:    "https://graph.qq.com/oauth2.0/token",
		UserinfoURL: "https://graph.qq.com/user/get_user_info",
		Scopes:      []string{"get_user_info"},
	},
	ProviderCustom: {
		Type:     ProviderCustom,
		Protocol: ProtocolOIDC,
		Scopes:   []string{"openid", "email", "profile"},
	},
}

// GetProviderProfile 返回 provider 模板；未知类型返回 ok=false。
func GetProviderProfile(providerType string) (ProviderProfile, bool) {
	p, ok := providerProfiles[providerType]
	return p, ok
}

// KnownProviderTypes 返回全部内置 provider 类型。
func KnownProviderTypes() []string {
	return []string{ProviderGoogle, ProviderGitHub, ProviderWeChat, ProviderDingTalk, ProviderFeishu, ProviderQQ, ProviderCustom}
}

// IsKnownProviderType 判断 provider 类型是否受支持。
func IsKnownProviderType(t string) bool {
	_, ok := providerProfiles[t]
	return ok
}

// ValidProtocol 判断协议取值合法。
func ValidProtocol(p string) bool {
	return p == ProtocolOIDC || p == ProtocolOAuth2
}

// ResolveEndpoints 用模板缺省值补齐 provider 端点：
// 显式覆盖（override 非空）优先于模板。issuer 同理。
func ResolveEndpoints(providerType string, issuer, authURL, tokenURL, userinfoURL string) (string, string, string, string) {
	profile, ok := providerProfiles[providerType]
	if !ok {
		return issuer, authURL, tokenURL, userinfoURL
	}
	if issuer == "" {
		issuer = profile.Issuer
	}
	if authURL == "" {
		authURL = profile.AuthURL
	}
	if tokenURL == "" {
		tokenURL = profile.TokenURL
	}
	if userinfoURL == "" {
		userinfoURL = profile.UserinfoURL
	}
	return issuer, authURL, tokenURL, userinfoURL
}

// DefaultScopes 返回 provider 模板默认 scopes（无模板时用 OIDC 缺省）。
func DefaultScopes(providerType string) []string {
	if profile, ok := providerProfiles[providerType]; ok && len(profile.Scopes) > 0 {
		return profile.Scopes
	}
	return []string{"openid", "email", "profile"}
}

// wechatMPAuthURL 公众号网页授权端点（extra.mode=mp）。
const wechatMPAuthURL = "https://open.weixin.qq.com/connect/oauth2/authorize"

// wechatEffectiveAuthURL 微信授权端点：extra.mode=mp 切换为公众号网页授权。
func wechatEffectiveAuthURL(authURL string, extra map[string]string) string {
	if extra["mode"] == "mp" {
		return wechatMPAuthURL
	}
	return authURL
}
