package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestStripJSONComments(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"leading line comment", "// c\n{\"a\":1}", "\n{\"a\":1}"},
		{"trailing line comment", "{\"a\":1} // c", "{\"a\":1} "},
		{"block comment", "[1,/*x*/2]", "[1, 2]"},
		{"comment markers inside string are kept", `{"u":"http://x/*y*/"}`, `{"u":"http://x/*y*/"}`},
		{"escaped quote does not end string", `{"u":"a\"//b"}`, `{"u":"a\"//b"}`},
		{"plain json untouched", `{"a":1}`, `{"a":1}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := string(stripJSONComments([]byte(tc.in))); got != tc.want {
				t.Fatalf("stripJSONComments(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestStripJSONCommentsDropsBOM(t *testing.T) {
	in := append([]byte{0xEF, 0xBB, 0xBF}, []byte(`{"a":1}`)...)
	if got := string(stripJSONComments(in)); got != `{"a":1}` {
		t.Fatalf("BOM not stripped: %q", got)
	}
}

// 这条用例回归的是 vendor/规划.md §3.3-4：仓库自带的 config/config.json 首行是 `//`
// 注释，原实现直接 json.Unmarshal ⇒ 解析失败 ⇒ 6 个键全部静默失效。
func TestShippedConfigJSONIsParseableJSONC(t *testing.T) {
	data, err := os.ReadFile("config.json")
	if err != nil {
		t.Fatalf("read shipped config.json: %v", err)
	}
	var cf ConfigFile
	if err := json.Unmarshal(stripJSONComments(data), &cf); err != nil {
		t.Fatalf("shipped config/config.json must stay parseable as JSONC: %v", err)
	}
	if cf.Port == "" || cf.LogLevel == "" || cf.StorageBackend == "" {
		t.Fatalf("shipped config.json lost its values: %+v", cf)
	}
}

func writeConfigFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write temp config: %v", err)
	}
	return path
}

// JSONC 文件的每个键都要真的生效（而不是只留一行 stderr warning）。
func TestLoadConfigFileAppliesJSONCValues(t *testing.T) {
	path := writeConfigFile(t, "// 说明：本文件是 JSONC\n"+`{
  "port": "19090",
  "log_level": "debug",
  "postgres_max_conn": 33,
  "postgres_min_conn": 3,
  "rate_limit_rpm": 55,
  "storage_backend": "s3"
}`)
	t.Setenv("CONFIG_FILE", path)

	want := map[string]string{
		"PORT":              "19090",
		"LOG_LEVEL":         "debug",
		"POSTGRES_MAX_CONN": "33",
		"POSTGRES_MIN_CONN": "3",
		"RATE_LIMIT_RPM":    "55",
		"STORAGE_BACKEND":   "s3",
	}
	for k := range want {
		t.Setenv(k, "")
	}

	loadConfigFile()

	for k, v := range want {
		if got := os.Getenv(k); got != v {
			t.Errorf("%s = %q, want %q", k, got, v)
		}
	}
}

// 环境变量/`.env` 优先级高于配置文件（loadDotEnv 先跑，setIfNot 只在为空时写入）。
func TestLoadConfigFileDoesNotOverrideEnv(t *testing.T) {
	path := writeConfigFile(t, `{"port": "19090"}`)
	t.Setenv("CONFIG_FILE", path)
	t.Setenv("PORT", "12345")

	loadConfigFile()

	if got := os.Getenv("PORT"); got != "12345" {
		t.Fatalf("env must win over config file, got PORT=%q", got)
	}
}

// 去注释不等于放宽语法：真的坏 JSON 仍要**显式失败**且一个键都不写。
func TestLoadConfigFileInvalidJSONAppliesNothing(t *testing.T) {
	path := writeConfigFile(t, `{"port": "19090",,}`)
	t.Setenv("CONFIG_FILE", path)
	t.Setenv("PORT", "")

	loadConfigFile()

	if got := os.Getenv("PORT"); got != "" {
		t.Fatalf("invalid JSON must not apply values, got PORT=%q", got)
	}
}
