package diagnostics

import "testing"

func TestSummarizePrecedence(t *testing.T) {
	ok := Check{Status: StatusOK}
	warn := Check{Status: StatusWarn}
	fail := Check{Status: StatusFail}
	skip := Check{Status: StatusSkipped}

	cases := []struct {
		name   string
		checks []Check
		want   Status
	}{
		{"all ok", []Check{ok, ok}, StatusOK},
		{"warn beats ok", []Check{ok, warn}, StatusWarn},
		{"fail beats warn", []Check{warn, fail}, StatusFail},
		{"empty is skipped", nil, StatusSkipped},
		// 关键：全 skipped 不能报 ok（"什么都没查"≠"一切正常"）。
		{"all skipped is skipped", []Check{skip, skip}, StatusSkipped},
		{"ok beside skipped is ok", []Check{ok, skip}, StatusOK},
	}
	for _, c := range cases {
		if got := Summarize(c.checks); got != c.want {
			t.Errorf("%s: want %s, got %s", c.name, c.want, got)
		}
	}
}

func TestBuildSortsAndCounts(t *testing.T) {
	rep := Build([]Check{
		{ID: "zzz", Category: CategoryCapability, Status: StatusOK},
		{ID: "bbb", Category: CategoryConfig, Status: StatusWarn},
		{ID: "aaa", Category: CategoryDependency, Status: StatusFail},
	}, 123)
	if rep.GeneratedAt != 123 {
		t.Fatalf("timestamp not passed through")
	}
	if rep.Status != StatusFail {
		t.Fatalf("summary must be fail, got %s", rep.Status)
	}
	// dependency 先于 config，config 先于 capability
	got := []string{rep.Checks[0].ID, rep.Checks[1].ID, rep.Checks[2].ID}
	want := []string{"aaa", "bbb", "zzz"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order mismatch: got %v want %v", got, want)
		}
	}
	if rep.Counts["fail"] != 1 || rep.Counts["warn"] != 1 || rep.Counts["ok"] != 1 {
		t.Fatalf("counts wrong: %+v", rep.Counts)
	}
}

func find(checks []Check, id string) (Check, bool) {
	for _, c := range checks {
		if c.ID == id {
			return c, true
		}
	}
	return Check{}, false
}

func TestCheckConfigFailsOnShortAppSecret(t *testing.T) {
	checks := CheckConfig(ConfigFacts{AppSecretSet: true, AppSecretLen: 8})
	c, ok := find(checks, "config.app_secret")
	if !ok || c.Status != StatusFail {
		t.Fatalf("short APP_SECRET must fail: %+v", c)
	}
	if c.Remediation == "" {
		t.Fatalf("a fail must carry a remediation")
	}
}

func TestCheckConfigFailsOnMissingAppSecret(t *testing.T) {
	checks := CheckConfig(ConfigFacts{})
	c, _ := find(checks, "config.app_secret")
	if c.Status != StatusFail {
		t.Fatalf("missing APP_SECRET must fail: %+v", c)
	}
}

func TestCheckConfigWarnsOnDegradedMode(t *testing.T) {
	checks := CheckConfig(ConfigFacts{AppSecretSet: true, AppSecretLen: 40, DegradedMode: true})
	c, _ := find(checks, "config.degraded_mode")
	if c.Status != StatusWarn {
		t.Fatalf("degraded mode must warn: %+v", c)
	}
}

func TestCheckConfigFailsOnIncompleteS3(t *testing.T) {
	checks := CheckConfig(ConfigFacts{
		AppSecretSet: true, AppSecretLen: 40,
		StorageBackend: "s3", S3Endpoint: "oss.example.com", S3Bucket: "b",
		S3AccessKey: true, // S3_SECRET_KEY 故意缺失
	})
	c, _ := find(checks, "config.storage_s3")
	if c.Status != StatusFail {
		t.Fatalf("incomplete S3 must fail: %+v", c)
	}
	if !contains(c.Detail, "S3_SECRET_KEY") {
		t.Fatalf("detail must name the missing variable: %s", c.Detail)
	}
}

func TestCheckConfigOkForCompleteS3(t *testing.T) {
	checks := CheckConfig(ConfigFacts{
		AppSecretSet: true, AppSecretLen: 40, StorageBackend: "s3",
		S3Endpoint: "e", S3Bucket: "b", S3AccessKey: true, S3SecretKey: true,
	})
	c, _ := find(checks, "config.storage_s3")
	if c.Status != StatusOK {
		t.Fatalf("complete S3 must be ok: %+v", c)
	}
}

func TestCheckConfigFailsWhenSandboxServiceHasNoURL(t *testing.T) {
	checks := CheckConfig(ConfigFacts{AppSecretSet: true, AppSecretLen: 40, SandboxBackend: "service"})
	c, _ := find(checks, "config.sandbox")
	if c.Status != StatusFail {
		t.Fatalf("service sandbox without URL must fail: %+v", c)
	}
}

func TestCheckConfigWarnsOnEmptyTrustedProxy(t *testing.T) {
	checks := CheckConfig(ConfigFacts{AppSecretSet: true, AppSecretLen: 40})
	c, _ := find(checks, "config.trusted_proxy")
	if c.Status != StatusWarn {
		t.Fatalf("empty TRUSTED_PROXY_CIDRS must warn: %+v", c)
	}
}

func TestCheckDependenciesFailWhenUnreached(t *testing.T) {
	checks := CheckDependencies([]Dep{
		{ID: "dep.db", Title: "PostgreSQL", Reached: true, Detail: "ok"},
		{ID: "dep.redis", Title: "Redis", Reached: false, Detail: "dial tcp: refused", Fix: "起 Redis 或改 REDIS_URL"},
	})
	db, _ := find(checks, "dep.db")
	rd, _ := find(checks, "dep.redis")
	if db.Status != StatusOK || rd.Status != StatusFail {
		t.Fatalf("dependency mapping wrong: %+v %+v", db, rd)
	}
	if rd.Remediation == "" {
		t.Fatalf("unreached dependency must carry a fix")
	}
	if db.Remediation != "" {
		t.Fatalf("reached dependency must not carry a fix")
	}
}

func TestCheckCapabilities(t *testing.T) {
	checks := CheckCapabilities([]Capability{
		{ID: "cap.models", Title: "可用模型", Status: StatusFail, Detail: "0 个", Fix: "配置 provider key"},
	})
	if len(checks) != 1 || checks[0].Category != CategoryCapability || checks[0].Status != StatusFail {
		t.Fatalf("capability mapping wrong: %+v", checks)
	}
}

func TestEveryFailCarriesRemediation(t *testing.T) {
	// 不变量：**fail 必须给补救办法** —— 只报"坏了"而不说怎么办，等于把问题丢回给用户。
	facts := []ConfigFacts{
		{}, // 几乎什么都没配
		{AppSecretSet: true, AppSecretLen: 8, StorageBackend: "s3", SandboxBackend: "service"},
	}
	for _, f := range facts {
		for _, c := range CheckConfig(f) {
			if c.Status == StatusFail && c.Remediation == "" {
				t.Errorf("%s is fail but has no remediation", c.ID)
			}
		}
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (func() bool {
		for i := 0; i+len(sub) <= len(s); i++ {
			if s[i:i+len(sub)] == sub {
				return true
			}
		}
		return false
	})()
}
