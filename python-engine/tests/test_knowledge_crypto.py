"""P1 数据层验收：用户级密钥加解密 + 原文对象存储。

跨语言向量 ``CIPHER`` 由固定 nonce（00..0b）与固定 ``APP_SECRET`` 生成；Go 侧
``internal/settings`` 必须能解出同一明文 —— 这条断言是"两侧算法不漂移"的锚点。
"""

from __future__ import annotations

import base64

import pytest

from app.knowledge import blob, crypto

SECRET = "chiron-test-app-secret-0123456789"
PLAIN = "sk-pageindex-test-abcdef"
CIPHER = (
    "v1:AAECAwQFBgcICQoL6/ZxeTz6+PTHhr3MZtxut3G2CIPpCKIhjGyCm0g7mY8pCcqxi5kyHg=="
)


# ── crypto ──


def test_roundtrip() -> None:
    encrypted = crypto.encrypt_secret(PLAIN, SECRET)
    assert encrypted.startswith("v1:")
    assert crypto.decrypt_secret(encrypted, SECRET) == PLAIN


def test_go_compatible_vector() -> None:
    assert crypto.decrypt_secret(CIPHER, SECRET) == PLAIN


def test_wrong_secret_fails() -> None:
    encrypted = crypto.encrypt_secret(PLAIN, SECRET)
    with pytest.raises(crypto.SecretCryptoError):
        crypto.decrypt_secret(encrypted, "another-secret")


def test_tampered_ciphertext_fails() -> None:
    encrypted = crypto.encrypt_secret(PLAIN, SECRET)
    raw = bytearray(base64.b64decode(encrypted[len(crypto.CIPHER_VERSION):]))
    raw[-1] ^= 0xFF
    tampered = crypto.CIPHER_VERSION + base64.b64encode(bytes(raw)).decode()
    with pytest.raises(crypto.SecretCryptoError):
        crypto.decrypt_secret(tampered, SECRET)


def test_missing_secret_is_rejected() -> None:
    assert crypto.encryption_available("") is False
    with pytest.raises(crypto.SecretCryptoError):
        crypto.encrypt_secret(PLAIN, "")


def test_fingerprint_is_stable_and_short() -> None:
    assert crypto.key_fingerprint(PLAIN) == crypto.key_fingerprint(PLAIN)
    assert len(crypto.key_fingerprint(PLAIN)) == 16
    assert crypto.key_fingerprint(PLAIN) != PLAIN


# ── blob ──


def test_local_blob_roundtrip(tmp_path) -> None:
    store = blob.LocalBlobStore(str(tmp_path))
    key = blob.build_key("t1", "kb1", "doc1", "a b/c?.pdf")
    store.put(key, b"hello", "application/pdf")
    assert store.exists(key)
    assert store.get(key) == b"hello"
    store.delete(key)
    assert not store.exists(key)


@pytest.mark.parametrize("bad_key", ["../x", "/abs/path", "a/../../b", ""])
def test_blob_key_rejects_traversal(tmp_path, bad_key: str) -> None:
    store = blob.LocalBlobStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.get(bad_key)


def test_build_key_sanitizes_filename() -> None:
    key = blob.build_key("t1", "kb1", "doc1", "..\\evil name.pdf")
    assert ".." not in key
    assert " " not in key
    assert key.startswith("t1/kb1/doc1/")
