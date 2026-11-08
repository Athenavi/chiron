module github.com/athenavi/chiron

go 1.26.10

// 上面这个补丁号**必须跟着补丁走**，别写成 1.26.0 这种「小版本就够」的形态：
//
//   govulncheck 实测表明 go1.26.4 的标准库有 21 处**可达**漏洞（crypto/x509 中间证书
//   校验、net/http 的 HTTP/2 HPACK 竞态与 Punycode 校验、tls 等），最高需 1.26.10 才修完。
//   这些漏洞不在我们 import 的模块里，**换依赖版本修不掉** —— 只有换编译器/标准库。
//
//   而 go 指令正是工具链版本的单一真相源：CI 的 setup-go 走 `go-version-file: go.mod`，
//   本地 GOTOOLCHAIN=auto 也按它自动取对应补丁版，Dockerfile 的 golang:1.26-alpine
//   是滚动 tag。写低补丁号 ⇒ 上面那条通路全都会装回带漏洞的版本。
//
// 升级时同步跑 `govulncheck ./...` 确认清零。

require (
	github.com/coreos/go-oidc/v3 v3.20.0
	github.com/golang-jwt/jwt/v5 v5.2.2
	github.com/google/uuid v1.6.0
	github.com/gorilla/websocket v1.5.3
	github.com/jackc/pgx/v5 v5.10.0
	github.com/minio/minio-go/v7 v7.0.65
	github.com/redis/go-redis/v9 v9.21.0
	github.com/robfig/cron/v3 v3.0.1
	github.com/spf13/cobra v1.10.2
	github.com/wechatpay-apiv3/wechatpay-go v0.2.21
	go.opentelemetry.io/otel v1.46.0
	golang.org/x/crypto v0.58.0
	golang.org/x/oauth2 v0.36.0
	golang.org/x/text v0.43.0
	gopkg.in/yaml.v3 v3.0.1
)

require (
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/dustin/go-humanize v1.0.1 // indirect
	github.com/go-jose/go-jose/v4 v4.1.6 // indirect
	github.com/inconshreveable/mousetrap v1.1.0 // indirect
	github.com/jackc/pgpassfile v1.0.0 // indirect
	github.com/jackc/pgservicefile v0.0.0-20240606120523-5a60cdf6a761 // indirect
	github.com/jackc/puddle/v2 v2.2.2 // indirect
	github.com/json-iterator/go v1.1.12 // indirect
	github.com/klauspost/compress v1.18.7 // indirect
	github.com/klauspost/cpuid/v2 v2.3.0 // indirect
	github.com/kr/pretty v0.3.1 // indirect
	github.com/minio/md5-simd v1.1.2 // indirect
	github.com/minio/sha256-simd v1.0.1 // indirect
	github.com/modern-go/concurrent v0.0.0-20180306012644-bacd9c7ef1dd // indirect
	github.com/modern-go/reflect2 v1.0.2 // indirect
	github.com/rs/xid v1.6.0 // indirect
	github.com/sirupsen/logrus v1.9.3 // indirect
	github.com/spf13/pflag v1.0.10 // indirect
	go.opentelemetry.io/otel/trace v1.46.0 // indirect
	go.uber.org/atomic v1.11.0 // indirect
	golang.org/x/net v0.62.0 // indirect
	golang.org/x/sync v0.24.0 // indirect
	golang.org/x/sys v0.49.0 // indirect
	gopkg.in/ini.v1 v1.67.2 // indirect
)
