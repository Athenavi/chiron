package settings

import "testing"

// 跨语言锚点：这条密文由 Python 侧生成（固定 nonce 00..0b + 固定 APP_SECRET），
// 生成脚本与同一条断言见 python-engine/tests/test_knowledge_crypto.py。
//
// 两侧算法一旦漂移（派生域、nonce 长度、base64 变体、拼接顺序），用户级 Cloud key
// 就会"网关写进去、引擎解不开"，而这类失败只在保存密钥之后才暴露 —— 所以用固定向量钉住。
func TestDecryptPythonCompatVector(t *testing.T) {
	const appSecret = "chiron-test-app-secret-0123456789"
	const cipher = "v1:AAECAwQFBgcICQoL6/ZxeTz6+PTHhr3MZtxut3G2CIPpCKIhjGyCm0g7mY8pCcqxi5kyHg=="
	const want = "sk-pageindex-test-abcdef"

	s := &Store{aead: newAEAD(appSecret)}
	got, err := s.DecryptString(cipher)
	if err != nil {
		t.Fatalf("DecryptString(%q): %v", cipher, err)
	}
	if got != want {
		t.Fatalf("plaintext = %q, want %q", got, want)
	}
}
