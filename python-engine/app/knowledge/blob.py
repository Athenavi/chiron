"""知识库原文的对象存储抽象。

**原文不进 PG**。旧实现把上传文件塞在 ``knowledge_documents.content``（BYTEA），在 SaaS
部署下有三个绕不过去的代价：大文件放大 WAL 与备份、迁移/升级要搬全量原文、存储无法独立
扩容。这里只留 ``storage_key``，字节放对象存储。

**多实例是硬约束**。``local`` 后端必须指向所有 python-engine 副本都可见的共享卷
（NFS / RWX PVC / 挂载桶）；否则 A 实例建立的索引与原文，B 实例读不到 —— 表现是"同一份
文档时而能查、时而 404"。生产环境建议 ``s3`` 后端；``local`` 只作为单机/开发默认。

配置（均可用 ``KNOWLEDGE_*`` 覆盖，未配时回落到通用 S3 变量，与 ``app/media/store.py``
同一套部署口径）::

    KNOWLEDGE_BLOB_BACKEND   local | s3（默认跟随 MEDIA_STORE_BACKEND，再缺省 local）
    KNOWLEDGE_LOCAL_PATH     local 根目录（默认 data/knowledge）
    KNOWLEDGE_S3_BUCKET      桶名（未配则回落 S3_BUCKET）
    KNOWLEDGE_S3_ENDPOINT_URL / KNOWLEDGE_S3_ACCESS_KEY / KNOWLEDGE_S3_SECRET_KEY
    KNOWLEDGE_S3_PREFIX      对象前缀（默认 knowledge/）
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Protocol

__all__ = [
    "BlobStore",
    "LocalBlobStore",
    "S3BlobStore",
    "build_key",
    "create_blob_store",
    "get_blob_store",
    "set_blob_store",
    "validate_key",
]

# key 只允许安全字符；拒绝 ``..``、绝对路径与空段，避免 local 后端被路径穿越。
_KEY_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]{1,512}$")
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "") -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ValueError(f"invalid blob key: {key!r}")
    return key


def build_key(tenant_id: str, kb_id: str, doc_id: str, filename: str) -> str:
    """按租户/库/文档分区；文件名只保留安全字符，避免覆盖同一文档的不同附件。"""
    safe_name = _UNSAFE_NAME_RE.sub("_", filename or "file").strip("._") or "file"
    return f"{tenant_id}/{kb_id}/{doc_id}/{safe_name[:180]}"


class LocalBlobStore:
    """共享卷上的对象存储（开发/单机默认）。"""

    def __init__(self, root: str) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise ValueError(f"blob key escapes storage root: {key!r}")
        return path

    def put(self, key: str, data: bytes, content_type: str = "") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：并发读不会看到半截文件
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3BlobStore:
    """S3 兼容对象存储（AWS S3 / MinIO）。生产环境推荐。"""

    def __init__(
        self,
        bucket: str,
        prefix: str = "knowledge/",
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        import boto3
        from botocore.config import Config as BotoConfig

        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/"
        # access_key/secret_key 为 None 时走 boto3 默认凭据链（env / IAM role），
        # 与 app/media/s3_store.py 的口径一致。
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(
                retries={"max_attempts": 3, "mode": "standard"},
                signature_version="s3v4",
            ),
        )

    def _object_key(self, key: str) -> str:
        return self._prefix + validate_key(key)

    def put(self, key: str, data: bytes, content_type: str = "") -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._object_key(key),
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )

    def get(self, key: str) -> bytes:
        resp = self._s3.get_object(Bucket=self._bucket, Key=self._object_key(key))
        return resp["Body"].read()

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=self._object_key(key))

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._object_key(key))
            return True
        except Exception:  # noqa: BLE001 — 404/权限都按"不可用"处理，由调用方决定
            return False


_current_store: BlobStore | None = None
_store_lock = threading.Lock()


def create_blob_store() -> BlobStore:
    backend = os.getenv(
        "KNOWLEDGE_BLOB_BACKEND", os.getenv("MEDIA_STORE_BACKEND", "local")
    ).lower()
    if backend == "s3":
        bucket = os.getenv("KNOWLEDGE_S3_BUCKET") or os.getenv("S3_BUCKET", "")
        if not bucket:
            raise ValueError(
                "KNOWLEDGE_S3_BUCKET (or S3_BUCKET) is required when blob backend is s3"
            )
        return S3BlobStore(
            bucket=bucket,
            prefix=os.getenv("KNOWLEDGE_S3_PREFIX", "knowledge/"),
            endpoint_url=os.getenv("KNOWLEDGE_S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL"),
            access_key=os.getenv("KNOWLEDGE_S3_ACCESS_KEY") or os.getenv("S3_ACCESS_KEY"),
            secret_key=os.getenv("KNOWLEDGE_S3_SECRET_KEY") or os.getenv("S3_SECRET_KEY"),
        )
    return LocalBlobStore(os.getenv("KNOWLEDGE_LOCAL_PATH", os.path.join("data", "knowledge")))


def get_blob_store() -> BlobStore:
    """进程内当前后端（与 app/media/store.py 的 get_store 同形，支持热替换）。"""
    global _current_store
    if _current_store is None:
        with _store_lock:
            if _current_store is None:
                _current_store = create_blob_store()
    return _current_store


def set_blob_store(store: BlobStore) -> None:
    global _current_store
    with _store_lock:
        _current_store = store
