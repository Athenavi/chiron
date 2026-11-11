module github.com/athenavi/chiron

go 1.26.11

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
	github.com/coreos/go-oidc/v3 v3.21.0
	github.com/golang-jwt/jwt/v5 v5.3.1
	github.com/google/uuid v1.6.0
	github.com/gorilla/websocket v1.5.3
	github.com/jackc/pgx/v5 v5.12.0
	github.com/minio/minio-go/v7 v7.4.1
	github.com/pquerna/otp v1.5.0
	github.com/redis/go-redis/v9 v9.23.0
	github.com/robfig/cron/v3 v3.0.1
	github.com/wechatpay-apiv3/wechatpay-go v0.2.21
	go.opentelemetry.io/otel v1.47.0
	golang.org/x/crypto v0.58.1-0.20261009181718-2f1f834330b2
	golang.org/x/oauth2 v0.38.0
	golang.org/x/text v0.44.0
)

require (
	github.com/boombuler/barcode v1.0.1-0.20190219062509-6c824513bacc // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/dustin/go-humanize v1.1.0 // indirect
	github.com/go-jose/go-jose/v4 v4.1.9 // indirect
	github.com/jackc/pgpassfile v1.0.0 // indirect
	github.com/jackc/pgservicefile v0.0.0-20240606120523-5a60cdf6a761 // indirect
	github.com/jackc/puddle/v2 v2.2.3 // indirect
	github.com/klauspost/compress v1.20.4 // indirect
	github.com/klauspost/cpuid/v2 v2.4.2 // indirect
	github.com/klauspost/crc32 v1.3.0 // indirect
	github.com/minio/crc64nvme v1.1.1 // indirect
	github.com/minio/md5-simd v1.1.2 // indirect
	github.com/philhofer/fwd v1.2.1 // indirect
	github.com/rs/xid v1.6.0 // indirect
	github.com/tinylib/msgp v1.6.5 // indirect
	github.com/zeebo/xxh3 v1.1.0 // indirect
	go.opentelemetry.io/otel/trace v1.47.0 // indirect
	go.uber.org/atomic v1.12.0 // indirect
	go.yaml.in/yaml/v3 v3.0.6 // indirect
	golang.org/x/net v0.62.0 // indirect
	golang.org/x/sync v0.25.0 // indirect
	golang.org/x/sys v0.50.0 // indirect
	gopkg.in/ini.v1 v1.67.3 // indirect
)
