"""S3 存储后端实现。

使用 boto3 将媒体资产存储到 S3 兼容服务（AWS S3、MinIO 等）。
元数据存储在 S3 对象的 metadata 中，无需额外数据库。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from app.media.store import Asset, BaseStore

logger = logging.getLogger(__name__)


class S3Store(BaseStore):
    """S3 存储后端。"""

    def __init__(
        self,
        bucket: str,
        prefix: str = "media/",
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/"
        # access_key/secret_key 为 None 时走 boto3 默认凭据链（env / IAM role）；
        # 后台「存储配置」下发的凭据则显式传入，让引擎随后台配置写同一个桶。
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
        self._index_key = f"{self._prefix}metadata.json"
        self._assets: dict[str, Asset] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=self._index_key)
            data = json.loads(resp["Body"].read().decode("utf-8"))
            for item in data:
                a = Asset(**item)
                self._assets[a.id] = a
        except self._s3.exceptions.NoSuchKey:
            pass
        except Exception:
            logger.warning(
                "Failed to load S3 metadata from %s/%s, starting with empty store",
                self._bucket,
                self._index_key,
            )

    def _save(self) -> None:
        with self._lock:
            data = [
                {
                    "id": a.id,
                    "name": a.name,
                    "file_url": a.file_url,
                    "type": a.type,
                    "category": a.category,
                    "tags": a.tags,
                    "size": a.size,
                    "format": a.format,
                    "width": a.width,
                    "height": a.height,
                    "created_at": a.created_at,
                }
                for a in self._assets.values()
            ]
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._index_key,
            Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )

    def write(
        self,
        name: str,
        content: bytes,
        asset_type: str = "text",
        category: str = "generated",
        tags: list[str] | None = None,
        fmt: str = "",
        width: int = 0,
        height: int = 0,
    ) -> Asset:
        asset_id = uuid.uuid4().hex[:12]
        prefix = asset_id[:8]
        safe_name = name.replace("/", "_").replace("\\", "_")
        key = f"{self._prefix}generated/{prefix}_{safe_name}"

        content_type = fmt if fmt else "application/octet-stream"
        extra_args: dict[str, Any] = {"ContentType": content_type}

        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content,
            **extra_args,
        )

        # 构造 S3 URL（兼容 AWS 和自定义端点）
        if endpoint_url := self._s3.meta.endpoint_url:
            file_url = f"{endpoint_url}/{self._bucket}/{key}"
        else:
            file_url = f"https://{self._bucket}.s3.amazonaws.com/{key}"

        asset = Asset(
            id=asset_id,
            name=name,
            file_url=file_url,
            type=asset_type,
            category=category,
            tags=tags or [],
            size=len(content),
            format=fmt,
            width=width,
            height=height,
        )
        with self._lock:
            self._assets[asset_id] = asset
            self._save()
        return asset

    def get(self, asset_id: str) -> Asset | None:
        with self._lock:
            return self._assets.get(asset_id)

    def list(self) -> list[Asset]:
        with self._lock:
            return list(self._assets.values())
