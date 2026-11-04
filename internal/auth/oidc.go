package auth

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"

	gooidc "github.com/coreos/go-oidc/v3/oidc"
	"golang.org/x/oauth2"
)

// ── state / nonce 编解码 ─────────────────────────────────
//
// state 令牌格式: base64url(JSON payload) + "." + base64url(HMAC-SHA256(payload))。
// payload 携带 provider_id / nonce / 过期时间，防 CSRF（HMAC 不可伪造）与重放（短 TTL）。

var (
	// ErrStateInvalid 表示 state 结构非法或 HMAC 校验失败（含篡改）。
	ErrStateInvalid = errors.New("invalid sso state")
	// ErrStateExpired 表示 state 已过期。
	ErrStateExpired = errors.New("sso state expired")
)

// StatePayload 是 state 令牌承载的数据。
type StatePayload struct {
	ProviderID string `json:"p"`
	Nonce      string `json:"n"`
	ExpiresAt  int64  `json:"e"` // unix 秒
	// Mode: "login"（默认，兼容旧令牌的空值）| "bind"（绑定已有账号）
	Mode string `json:"m,omitempty"`
	// UID 仅 bind 模式携带：发起绑定的已登录用户（state 由 authMW 保护的路由签发）
	UID string `json:"u,omitempty"`
}

// State 双模式常量。
const (
	StateModeLogin = "login"
	StateModeBind  = "bind"
)

// StateCodec 签发/校验 HMAC 签名的 SSO state 令牌。
type StateCodec struct {
	key []byte
	ttl time.Duration
	// now 可被测试替换，用于过期分支验证
	now func() time.Time
}

// NewStateCodec 构造 StateCodec。key 为签名密钥（≥1 字节），ttl 为有效期。
func NewStateCodec(key []byte, ttl time.Duration) *StateCodec {
	return NewStateCodecWithClock(key, ttl, time.Now)
}

// NewStateCodecWithClock 允许注入时钟（供测试验证过期分支）。
func NewStateCodecWithClock(key []byte, ttl time.Duration, now func() time.Time) *StateCodec {
	return &StateCodec{key: key, ttl: ttl, now: now}
}


// IssueMode 签发指定模式的 state（bind 模式须携带 uid）。
func (c *StateCodec) IssueMode(providerID, nonce, mode, uid string) (string, error) {
	if providerID == "" || nonce == "" {
		return "", errors.New("state codec: providerID and nonce are required")
	}
	switch mode {
	case StateModeLogin:
		uid = "" // login 模式不携带 uid
	case StateModeBind:
		if uid == "" {
			return "", errors.New("state codec: bind mode requires uid")
		}
	default:
		return "", errors.New("state codec: unknown mode " + mode)
	}
	payload := StatePayload{
		ProviderID: providerID,
		Nonce:      nonce,
		ExpiresAt:  c.now().Add(c.ttl).Unix(),
		Mode:       mode,
		UID:        uid,
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	body := base64.RawURLEncoding.EncodeToString(raw)
	return body + "." + c.sign(body), nil
}

// Verify 校验 state 令牌并返回 payload。
// 结构非法/HMAC 不匹配 → ErrStateInvalid；超过有效期 → ErrStateExpired。
func (c *StateCodec) Verify(state string) (*StatePayload, error) {
	body, sig, found := cutState(state)
	if !found {
		return nil, ErrStateInvalid
	}
	if !hmac.Equal([]byte(sig), []byte(c.sign(body))) {
		return nil, ErrStateInvalid
	}
	raw, err := base64.RawURLEncoding.DecodeString(body)
	if err != nil {
		return nil, ErrStateInvalid
	}
	var payload StatePayload
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, ErrStateInvalid
	}
	if payload.ProviderID == "" || payload.Nonce == "" {
		return nil, ErrStateInvalid
	}
	if c.now().Unix() > payload.ExpiresAt {
		return nil, ErrStateExpired
	}
	return &payload, nil
}

func cutState(state string) (body, sig string, found bool) {
	for i := len(state) - 1; i >= 0; i-- {
		if state[i] == '.' {
			return state[:i], state[i+1:], true
		}
	}
	return "", "", false
}

func (c *StateCodec) sign(body string) string {
	mac := hmac.New(sha256.New, c.key)
	mac.Write([]byte(body))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// RandomNonce 生成 16 字节随机十六进制 nonce。
func RandomNonce() (string, error) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return hex.EncodeToString(buf), nil
}

// ── OIDC IdP 交互 ────────────────────────────────────────

// OIDCProviderConfig 是发起 OIDC 流程所需的 provider 配置（secret 已解密）。
// protocol=oauth2 时 Issuer 可为空，改用 AuthURL/TokenURL/UserinfoURL。
type OIDCProviderConfig struct {
	Issuer       string
	ClientID     string
	ClientSecret string
	Scopes       []string
	RedirectURL  string

	// ── OAuth2 扩展（protocol=oauth2 时生效）──
	Protocol     string            // "oidc" | "oauth2"；空视为 oidc
	ProviderType string            // github/wechat/dingtalk/feishu/qq/custom
	AuthURL      string            // 授权端点（模板缺省自动填充）
	TokenURL     string            // token 端点
	UserinfoURL  string            // userinfo 端点
	Extra        map[string]string // provider 特有项（微信 mode=open|mp 等）
}

// IDTokenResult 是授权码换 token + 身份校验后的结果
// （OIDC 来自 id_token claims；OAuth2 来自 userinfo 接口）。
type IDTokenResult struct {
	Subject string
	Email   string
	// Roles 来自 IdP 的可选 "roles" claim（字符串或字符串数组），用于 role_mapping 匹配
	Roles []string
	// Name / AvatarURL / Phone 为 userinfo 可选字段（OAuth2 家族填充）
	Name      string
	AvatarURL string
	Phone     string
}

// OIDCExchanger 抽象与 IdP 的交互（发现/授权 URL/授权码交换/id_token 校验），
// 测试中可替换为 fake，无需真实网络。
type OIDCExchanger interface {
	// AuthURL 构造 IdP 授权页 URL（携带 state 与 nonce）。
	AuthURL(ctx context.Context, p *OIDCProviderConfig, state, nonce string) (string, error)
	// ExchangeAndVerify 用授权码换 token 并校验 id_token（aud/iss/exp + nonce）。
	ExchangeAndVerify(ctx context.Context, p *OIDCProviderConfig, code, expectedNonce string) (*IDTokenResult, error)
}

// RemoteOIDCExchanger 是基于 go-oidc 的真实实现，按 issuer 缓存发现结果。
type RemoteOIDCExchanger struct {
	mu        sync.Mutex
	providers map[string]*gooidc.Provider
}

func NewRemoteOIDCExchanger() *RemoteOIDCExchanger {
	return &RemoteOIDCExchanger{providers: make(map[string]*gooidc.Provider)}
}

func (e *RemoteOIDCExchanger) discover(ctx context.Context, p *OIDCProviderConfig) (*gooidc.Provider, *oauth2.Config, error) {
	e.mu.Lock()
	provider, ok := e.providers[p.Issuer]
	e.mu.Unlock()
	if !ok {
		var err error
		provider, err = gooidc.NewProvider(ctx, p.Issuer)
		if err != nil {
			return nil, nil, fmt.Errorf("oidc discovery %s: %w", p.Issuer, err)
		}
		e.mu.Lock()
		e.providers[p.Issuer] = provider
		e.mu.Unlock()
	}
	scopes := p.Scopes
	if len(scopes) == 0 {
		scopes = []string{gooidc.ScopeOpenID, "email", "profile"}
	}
	cfg := &oauth2.Config{
		ClientID:     p.ClientID,
		ClientSecret: p.ClientSecret,
		Endpoint:     provider.Endpoint(),
		RedirectURL:  p.RedirectURL,
		Scopes:       scopes,
	}
	return provider, cfg, nil
}

func (e *RemoteOIDCExchanger) AuthURL(ctx context.Context, p *OIDCProviderConfig, state, nonce string) (string, error) {
	_, cfg, err := e.discover(ctx, p)
	if err != nil {
		return "", err
	}
	return cfg.AuthCodeURL(state, gooidc.Nonce(nonce)), nil
}

func (e *RemoteOIDCExchanger) ExchangeAndVerify(ctx context.Context, p *OIDCProviderConfig, code, expectedNonce string) (*IDTokenResult, error) {
	provider, cfg, err := e.discover(ctx, p)
	if err != nil {
		return nil, err
	}
	token, err := cfg.Exchange(ctx, code)
	if err != nil {
		return nil, fmt.Errorf("oidc exchange: %w", err)
	}
	rawIDToken, ok := token.Extra("id_token").(string)
	if !ok || rawIDToken == "" {
		return nil, errors.New("oidc: id_token missing in token response")
	}
	idToken, err := provider.Verifier(&gooidc.Config{ClientID: p.ClientID}).Verify(ctx, rawIDToken)
	if err != nil {
		return nil, fmt.Errorf("oidc verify id_token: %w", err)
	}
	// go-oidc 不自动校验 nonce，这里手动恒等比较（防重放）
	if idToken.Nonce != expectedNonce {
		return nil, errors.New("oidc: id_token nonce mismatch")
	}

	result := &IDTokenResult{Subject: idToken.Subject}
	var claims struct {
		Email string `json:"email"`
		Roles any    `json:"roles"`
	}
	if err := idToken.Claims(&claims); err != nil {
		return nil, fmt.Errorf("oidc parse claims: %w", err)
	}
	result.Email = claims.Email
	result.Roles = normalizeRolesClaim(claims.Roles)
	return result, nil
}

// normalizeRolesClaim 将 IdP "roles" claim 归一化为字符串切片。
// 支持单字符串、字符串数组两种常见形态；其他形态返回空切片。
func normalizeRolesClaim(v any) []string {
	switch val := v.(type) {
	case string:
		if val == "" {
			return nil
		}
		return []string{val}
	case []any:
		out := make([]string, 0, len(val))
		for _, item := range val {
			if s, ok := item.(string); ok && s != "" {
				out = append(out, s)
			}
		}
		sort.Strings(out)
		return out
	default:
		return nil
	}
}
