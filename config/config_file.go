package config

import (
	"bytes"
	"encoding/json"
	"os"
	"strconv"
)

// ConfigFile is the JSON-serializable subset of Config for config file.
type ConfigFile struct {
	Port         string `json:"port,omitempty"`
	ReadTimeout  string `json:"read_timeout,omitempty"`
	WriteTimeout string `json:"write_timeout,omitempty"`
	IdleTimeout  string `json:"idle_timeout,omitempty"`

	PostgresDSN     string `json:"postgres_dsn,omitempty"`
	PostgresMaxConn *int   `json:"postgres_max_conn,omitempty"`
	PostgresMinConn *int   `json:"postgres_min_conn,omitempty"`

	RedisAddr     string `json:"redis_addr,omitempty"`
	RedisPassword string `json:"redis_password,omitempty"`
	RedisDB       *int   `json:"redis_db,omitempty"`

	JWTSecret     string `json:"jwt_secret,omitempty"`
	JWTExpiration string `json:"jwt_expiration,omitempty"`
	CORSOrigins   string `json:"cors_origins,omitempty"`

	// LLM 配置已迁移到 Python 引擎，此处保留存储配置字段
	StorageBackend string `json:"storage_backend,omitempty"`
	StorageRoot    string `json:"storage_root,omitempty"`
	S3Endpoint     string `json:"s3_endpoint,omitempty"`
	S3Bucket       string `json:"s3_bucket,omitempty"`
	S3AccessKey    string `json:"s3_access_key,omitempty"`
	S3SecretKey    string `json:"s3_secret_key,omitempty"`

	RateLimitRPM    *int   `json:"rate_limit_rpm,omitempty"`
	RateLimitGlobal *int   `json:"rate_limit_global,omitempty"`
	LogLevel        string `json:"log_level,omitempty"`
}

// configPath returns the config file path (default: config/config.json).
func configPath() string {
	if p := os.Getenv("CONFIG_FILE"); p != "" {
		return p
	}
	return "config/config.json"
}

// loadConfigFile reads a JSON config file and applies its values to env vars
// so they are picked up by Load(). Env vars and .env still take precedence.
func loadConfigFile() {
	path := configPath()
	// Search up the directory tree if the config file is not found at the relative path.
	if _, err := os.Stat(path); os.IsNotExist(err) {
		found := findFileUpward(path)
		if found != path {
			path = found
		}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return // file not found, skip
	}

	var cf ConfigFile
	if err := json.Unmarshal(stripJSONComments(data), &cf); err != nil {
		os.Stderr.WriteString("WARN: config file " + path + " parse error: " + err.Error() + "\n")
		return
	}

	// Apply each non-zero field as if it were an env var (env takes precedence)
	setIfNot("PORT", cf.Port)
	setIfNot("READ_TIMEOUT", cf.ReadTimeout)
	setIfNot("WRITE_TIMEOUT", cf.WriteTimeout)
	setIfNot("IDLE_TIMEOUT", cf.IdleTimeout)
	setIfNot("POSTGRES_DSN", cf.PostgresDSN)
	setIfNotInt("POSTGRES_MAX_CONN", cf.PostgresMaxConn)
	setIfNotInt("POSTGRES_MIN_CONN", cf.PostgresMinConn)
	setIfNot("REDIS_ADDR", cf.RedisAddr)
	setIfNot("REDIS_PASSWORD", cf.RedisPassword)
	setIfNotInt("REDIS_DB", cf.RedisDB)
	setIfNot("JWT_SECRET", cf.JWTSecret)
	setIfNot("JWT_EXPIRATION", cf.JWTExpiration)
	setIfNot("CORS_ORIGINS", cf.CORSOrigins)
	setIfNot("STORAGE_BACKEND", cf.StorageBackend)
	setIfNot("STORAGE_ROOT", cf.StorageRoot)
	setIfNot("S3_ENDPOINT", cf.S3Endpoint)
	setIfNot("S3_BUCKET", cf.S3Bucket)
	setIfNot("S3_ACCESS_KEY", cf.S3AccessKey)
	setIfNot("S3_SECRET_KEY", cf.S3SecretKey)
	setIfNotInt("RATE_LIMIT_RPM", cf.RateLimitRPM)
	setIfNotInt("RATE_LIMIT_GLOBAL", cf.RateLimitGlobal)
	setIfNot("LOG_LEVEL", cf.LogLevel)
}

func setIfNot(key, val string) {
	if val == "" {
		return
	}
	if os.Getenv(key) == "" {
		os.Setenv(key, val)
	}
}

func setIfNotInt(key string, val *int) {
	if val == nil {
		return
	}
	if os.Getenv(key) == "" {
		os.Setenv(key, strconv.Itoa(*val))
	}
}

// stripJSONComments 去掉 JSON 字符串之外的 `//` 行注释与 `/* */` 块注释，并丢弃
// UTF-8 BOM，使 config/config.json 这类 **JSONC** 文件能被 encoding/json 解析。
//
// 背景：config/config.json 首行就是 `//` 注释，而 loadConfigFile 原先直接
// json.Unmarshal ⇒ 解析失败 ⇒ 整个文件的 6 个键**静默失效**（只留一行 stderr
// warning），即 vendor/规划.md §3.3-4 与 §4「配置里有 ≠ 运行期生效」。
//
// 只做"去注释"这一件事：不解析、不校验、不放宽 JSON 语法（不做尾逗号等宽容处理），
// 语法错误仍由 json.Unmarshal 报出 —— 失败要显式（§1.3）。
func stripJSONComments(data []byte) []byte {
	// 允许带 BOM 的文件（Windows 编辑器的常见产物）；BOM 会让 json 直接报错。
	data = bytes.TrimPrefix(data, []byte{0xEF, 0xBB, 0xBF})

	out := make([]byte, 0, len(data))
	inString, escaped := false, false
	for i := 0; i < len(data); i++ {
		c := data[i]

		if inString {
			out = append(out, c)
			switch {
			case escaped:
				escaped = false
			case c == '\\':
				escaped = true
			case c == '"':
				inString = false
			}
			continue
		}

		switch {
		case c == '"':
			inString = true
			out = append(out, c)
		case c == '/' && i+1 < len(data) && data[i+1] == '/':
			// 行注释：吃掉到行尾（不含换行），保留换行以免两侧 token 粘连。
			for i < len(data) && data[i] != '\n' {
				i++
			}
			if i < len(data) {
				out = append(out, '\n')
			}
		case c == '/' && i+1 < len(data) && data[i+1] == '*':
			// 块注释：吃掉 `/*` … `*/`，用一个空格占位。
			i += 2
			for i+1 < len(data) && !(data[i] == '*' && data[i+1] == '/') {
				i++
			}
			i++ // 跳过结尾的 '/'
			out = append(out, ' ')
		default:
			out = append(out, c)
		}
	}
	return out
}
