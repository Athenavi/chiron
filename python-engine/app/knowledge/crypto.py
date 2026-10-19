"""知识库用户级密钥的加解密 —— 与 Go 侧 ``internal/settings`` 字节级同构。

为什么必须同构、不能各写各的:用户级 PageIndex Cloud key 由引擎（FastAPI）读写，而
``APP_SECRET`` 派生的 AEAD 是网关侧 ``internal/settings`` 与 ``internal/auth`` 已在用的
密钥体系。如果两侧算法有任何一处不同（派生域、nonce 长度、base64 变体、拼接顺序），
"网关写入、引擎读取"就会静默解不开 —— 而这类失败只会在用户保存密钥之后才暴露。

算法逐条对齐 ``internal/settings/settings.go``：

* 密钥 = ``SHA-256(b"chiron-settings:" + APP_SECRET)``；
* ``AES-256-GCM``，nonce **12 字节**随机；
* 密文串 = ``"v1:" + base64(nonce || ciphertext || tag)``（标准字母表 + padding）。

``APP_SECRET`` 为空时**不**回退到任何默认值：调用方必须显式判断
``encryption_available()``，然后拒绝保存密钥（与 Go 侧 ``EncryptEnabled()`` 一致），
否则会用一把空密钥把密文写进库。
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CIPHER_VERSION = "v1:"
NONCE_SIZE = 12

# 与 Go 侧 internal/settings 的派生域保持一致；不要改成 "chiron-encrypt:" ——
# 那是 internal/auth 给 SSO/短信凭据用的域，换了就解不开另一侧的密文。
_KEY_DOMAIN = b"chiron-settings:"


class SecretCryptoError(Exception):
    """密钥不可用、密文损坏或密钥不匹配。消息里不携带明文。"""


def encryption_available(app_secret: str) -> bool:
    """部署主密钥是否可用于加解密。"""
    return bool(app_secret)


def derive_key(app_secret: str) -> bytes:
    """由部署主密钥派生 32 字节 AES-256 密钥。"""
    if not app_secret:
        raise SecretCryptoError("APP_SECRET is not configured")
    return hashlib.sha256(_KEY_DOMAIN + app_secret.encode("utf-8")).digest()


def encrypt_secret(plaintext: str, app_secret: str) -> str:
    """加密为可落库的 ``v1:`` 密文串。"""
    key = derive_key(app_secret)
    nonce = os.urandom(NONCE_SIZE)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return CIPHER_VERSION + base64.b64encode(nonce + sealed).decode("ascii")


def decrypt_secret(ciphertext: str, app_secret: str) -> str:
    """解密 ``encrypt_secret`` 或 Go ``settings.EncryptString`` 产生的密文。"""
    key = derive_key(app_secret)
    if not isinstance(ciphertext, str) or not ciphertext.startswith(CIPHER_VERSION):
        raise SecretCryptoError("unsupported ciphertext version")
    try:
        raw = base64.b64decode(ciphertext[len(CIPHER_VERSION):], validate=True)
    except Exception as exc:  # noqa: BLE001 — 统一收敛为一种可预期的失败
        raise SecretCryptoError("invalid ciphertext encoding") from exc
    if len(raw) <= NONCE_SIZE:
        raise SecretCryptoError("invalid ciphertext")
    nonce, sealed = raw[:NONCE_SIZE], raw[NONCE_SIZE:]
    try:
        plain = AESGCM(key).decrypt(nonce, sealed, None)
    except Exception as exc:  # noqa: BLE001
        raise SecretCryptoError("decryption failed") from exc
    return plain.decode("utf-8")


def key_fingerprint(plaintext: str) -> str:
    """不可逆指纹，仅用于列表展示与去重；不参与解密。"""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()[:16]
