package settings

import (
	"strings"
	"testing"
)

// ── 主密钥轮换 ──
//
// 背景：密文与主密钥强绑定。运维轮换 APP_SECRET（密钥疑似泄露、例行轮换、
// 合规要求）后，库里所有已加密值会**永久不可解** —— 而失败要等到用户下次
// 读取配置时才暴露，表现是"provider key 全没了"。
//
// 本组用例钉住四个行为：
//  1. 配了 APP_SECRET_PREVIOUS 时，旧密钥加密的数据仍能读出；
//  2. 新数据用**新**密钥加密（轮换要真正生效，不是继续用旧钥匙写）；
//  3. 没配历史密钥时旧密文解不开（证明这个配置确实是必需的，
//     而不是"反正都能解"的巧合）；
//  4. 全部密钥都不匹配时返回错误而不是 panic 或返回垃圾。
//
// 注意每条用例都要显式设置/清除环境变量：t.Setenv 会自动还原，
// 但同一测试内多次切换需要显式调用。

// rotateStore 造一个"当前密钥为 cur、历史密钥为 prevList"的 Store。
func rotateStore(t *testing.T, cur string, prevList ...string) *Store {
	t.Helper()
	if len(prevList) == 0 {
		// 显式清空：避免宿主环境的 APP_SECRET_PREVIOUS 混进测试。
		t.Setenv(envAppSecretPrevious, "")
	} else {
		t.Setenv(envAppSecretPrevious, strings.Join(prevList, ","))
	}
	return New(nil, cur)
}

// 轮换后的核心场景：旧密钥加密 → 换新密钥 + 配历史密钥 → 仍能解密。
func TestKeyRotationDecryptsWithPreviousSecret(t *testing.T) {
	const oldSecret = "old-app-secret-0123456789abcdef"
	const newSecret = "new-app-secret-0123456789abcdef"
	const plaintext = "sk-live-provider-key-abcdef123456"

	// ① 用旧密钥加密（轮换前写入的数据）。
	cipherText, err := rotateStore(t, oldSecret).EncryptString(plaintext)
	if err != nil {
		t.Fatalf("旧密钥加密失败: %v", err)
	}

	// ② 轮换：当前密钥换成新的，旧密钥进 APP_SECRET_PREVIOUS。
	rotated := rotateStore(t, newSecret, oldSecret)
	got, err := rotated.DecryptString(cipherText)
	if err != nil {
		t.Fatalf("轮换后无法解密旧密文：轮换会变成数据丢失 —— %v", err)
	}
	if got != plaintext {
		t.Fatalf("解密结果 = %q，期望 %q", got, plaintext)
	}
}

// 没有历史密钥时必须解不开：否则说明"轮换"其实没换钥匙，
// 或者加密与密钥根本无关（那才是最糟的情况）。
func TestKeyRotationWithoutPreviousFails(t *testing.T) {
	const oldSecret = "old-app-secret-0123456789abcdef"
	const newSecret = "new-app-secret-0123456789abcdef"

	cipherText, err := rotateStore(t, oldSecret).EncryptString("secret-value")
	if err != nil {
		t.Fatalf("加密失败: %v", err)
	}

	// 只用新密钥、不配历史：应当失败。
	if _, err := rotateStore(t, newSecret).DecryptString(cipherText); err == nil {
		t.Fatal("未配 APP_SECRET_PREVIOUS 却能解密旧密文：说明主密钥没有真正参与加密")
	}
}

// 轮换后新写入的数据必须是**新密钥**加密的：否则轮换永远完成不了
// （每条新数据仍绑在旧钥匙上，历史密钥就永远不能摘掉）。
func TestKeyRotationEncryptsWithCurrentSecret(t *testing.T) {
	const oldSecret = "old-app-secret-0123456789abcdef"
	const newSecret = "new-app-secret-0123456789abcdef"

	rotated := rotateStore(t, newSecret, oldSecret)
	cipherText, err := rotated.EncryptString("fresh-value")
	if err != nil {
		t.Fatalf("加密失败: %v", err)
	}

	// 只配新密钥（摘掉历史）应该能解开 —— 证明密文确实是新钥匙写的。
	onlyNew := rotateStore(t, newSecret)
	if _, err := onlyNew.DecryptString(cipherText); err != nil {
		t.Fatalf("新写入的密文无法用新密钥单独解开：轮换无法收敛 —— %v", err)
	}
	// 只用旧密钥应当解不开。
	onlyOld := rotateStore(t, oldSecret)
	if _, err := onlyOld.DecryptString(cipherText); err == nil {
		t.Fatal("新密文用旧密钥也能解开：说明用的还是旧钥匙")
	}
}

// 多个历史密钥按"越新越靠前"的语义都能解。
func TestKeyRotationSupportsMultiplePreviousSecrets(t *testing.T) {
	const s1 = "secret-generation-1-0123456789"
	const s2 = "secret-generation-2-0123456789"
	const s3 = "secret-generation-3-0123456789"

	ct1, err := rotateStore(t, s1).EncryptString("value-from-gen1")
	if err != nil {
		t.Fatalf("加密失败: %v", err)
	}
	ct2, err := rotateStore(t, s2).EncryptString("value-from-gen2")
	if err != nil {
		t.Fatalf("加密失败: %v", err)
	}

	// 当前是 s3，历史按新→旧排列。
	cur := rotateStore(t, s3, s2, s1)
	for name, tc := range map[string]struct{ cipher, want string }{
		"gen1": {ct1, "value-from-gen1"},
		"gen2": {ct2, "value-from-gen2"},
	} {
		got, err := cur.DecryptString(tc.cipher)
		if err != nil {
			t.Fatalf("%s 密文无法解密: %v", name, err)
		}
		if got != tc.want {
			t.Fatalf("%s 解密结果 = %q，期望 %q", name, got, tc.want)
		}
	}
}

// 全都不匹配时返回错误，不能 panic 也不能返回空字符串冒充成功。
func TestDecryptWithNoMatchingKeyReturnsError(t *testing.T) {
	ct, err := rotateStore(t, "some-secret-0123456789abcdef").EncryptString("v")
	if err != nil {
		t.Fatalf("加密失败: %v", err)
	}

	other := rotateStore(t, "completely-different-secret-xyz", "yet-another-secret-abc")
	got, err := other.DecryptString(ct)
	if err == nil {
		t.Fatalf("不匹配却返回成功，明文=%q（应报错，否则会把垃圾当配置用）", got)
	}
	if got != "" {
		t.Fatalf("失败时应返回空串，得到 %q", got)
	}
}

// 空密钥 + 无历史：保持既有语义（ErrEncryptedKeyNotFound），
// 不要因为加了轮换逻辑就把"未配置"变成"解密失败"。
func TestDecryptWithoutAnyKeyKeepsExistingError(t *testing.T) {
	s := rotateStore(t, "")
	if _, err := s.DecryptString("v1:AAAA"); err != ErrEncryptedKeyNotFound {
		t.Fatalf("期望 ErrEncryptedKeyNotFound，得到 %v", err)
	}
}

// 畸形密文（非 base64、过短）不应 panic。
func TestDecryptGarbageDoesNotPanic(t *testing.T) {
	s := rotateStore(t, "secret-0123456789abcdef")
	for _, bad := range []string{"", "v1:", "v1:not-base64!!!", "v1:AAAA"} {
		if _, err := s.DecryptString(bad); err == nil {
			t.Fatalf("畸形密文 %q 竟然解密成功", bad)
		}
	}
}

// 历史密钥列表里的空白项要被跳过，不能因为末尾多个逗号就失去全部回退能力。
func TestPreviousSecretListToleratesWhitespace(t *testing.T) {
	const oldSecret = "old-app-secret-0123456789abcdef"
	const newSecret = "new-app-secret-0123456789abcdef"

	ct, err := rotateStore(t, oldSecret).EncryptString("value")
	if err != nil {
		t.Fatalf("加密失败: %v", err)
	}

	// 形如 " old , , " 的历史列表：应当仍能取出 old。
	rotated := rotateStore(t, newSecret, " "+oldSecret+" ", " ", "")
	if _, err := rotated.DecryptString(ct); err != nil {
		t.Fatalf("带空白的历史密钥列表未能回退解密: %v", err)
	}
}
