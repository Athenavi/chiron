"""服务侧的小工具：Redis 键、原文读取、Cloud key 解密。

**三件事都刻意重复实现**（引擎里有对应版本）。理由：本镜像**不装引擎依赖**
（pageindex → openai<3 与引擎的 openai 3.x 无法共存），所以不能 import 引擎的模块。
重复的代价由契约锁住：

* ``rkey`` 的前缀规则必须与引擎 ``app/redis_keys.py`` 一致（否则 worker 消费不到队列）；
* AES-GCM 派生与 ``internal/settings`` 逐字对齐（否则解不开用户 key）；
* 对象存储的 key 规则与引擎 ``app/knowledge/blob.py`` 一致（否则读不到原文）。

后两条各有跨端测试兜底（引擎侧 test_knowledge_crypto 的固定向量 / 本合同的契约测试）。
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

# ── Redis 键（与引擎 app/redis_keys.py 同规则：前缀 + 冒号）─────────────────


def rkey(name: str) -> str:
    prefix = (os.getenv("REDIS_KEY_PREFIX") or "").strip()
    return f"{prefix}:{name}" if prefix else name


# ── 原文读取（S3 或共享卷；key 由引擎生成）──────────────────────────────


class BlobMissing(RuntimeError):
    """原文对象不存在（对象存储被清空/迁移不完整）。"""


class BlobReader:
    """只读原文 —— worker 索引时需要字节，服务不需要（检索只拿 doc_id 与问题）。"""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg
        self._client: Any = None

    def _s3(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config as BotoConfig

            endpoint = self._cfg.s3_endpoint_url
            if not endpoint and self._cfg.s3_endpoint_host:
                scheme = "https" if self._cfg.s3_use_ssl else "http"
                endpoint = f"{scheme}://{self._cfg.s3_endpoint_host}"
            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint or None,
                aws_access_key_id=self._cfg.s3_access_key or None,
                aws_secret_access_key=self._cfg.s3_secret_key or None,
                config=BotoConfig(
                    retries={"max_attempts": 3, "mode": "standard"},
                    signature_version="s3v4",
                ),
            )
        return self._client

    def get(self, storage_key: str) -> bytes:
        if not storage_key:
            raise BlobMissing("storage_key 为空")
        if self._cfg.blob_backend == "s3":
            if not self._cfg.s3_bucket:
                raise BlobMissing("KNOWLEDGE_S3_BUCKET/S3_BUCKET 未配置")
            obj = f"{self._cfg.s3_prefix.rstrip('/')}/{storage_key.lstrip('/')}"
            resp = self._s3().get_object(Bucket=self._cfg.s3_bucket, Key=obj)
            return resp["Body"].read()
        root = Path(self._cfg.blob_local_path).expanduser().resolve()
        path = (root / storage_key).resolve()
        if root not in path.parents:
            raise BlobMissing(f"非法 storage_key: {storage_key!r}")
        return path.read_bytes()


# ── Cloud key 解密（与 Go internal/settings、引擎 app/knowledge/crypto.py 同构）──

_CIPHER_VERSION = "v1:"
_NONCE_SIZE = 12
_KEY_DOMAIN = b"chiron-settings:"


class SecretDecryptError(RuntimeError):
    """APP_SECRET 缺失或密文损坏/不匹配。消息里不携带明文。"""


def decrypt_secret(ciphertext: str, app_secret: str) -> str:
    if not app_secret:
        raise SecretDecryptError("APP_SECRET 未配置，无法解密用户凭据")
    if not isinstance(ciphertext, str) or not ciphertext.startswith(_CIPHER_VERSION):
        raise SecretDecryptError("不支持的密文版本")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = base64.b64decode(ciphertext[len(_CIPHER_VERSION):], validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SecretDecryptError("密文编码非法") from exc
    if len(raw) <= _NONCE_SIZE:
        raise SecretDecryptError("密文长度非法")
    key = hashlib.sha256(_KEY_DOMAIN + app_secret.encode("utf-8")).digest()
    nonce, sealed = raw[:_NONCE_SIZE], raw[_NONCE_SIZE:]
    try:
        return AESGCM(key).decrypt(nonce, sealed, None).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise SecretDecryptError("解密失败（APP_SECRET 是否与引擎一致？）") from exc
