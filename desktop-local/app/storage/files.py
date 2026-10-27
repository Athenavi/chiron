"""本地媒体库的磁盘落盘 + ``local_media`` 表访问（契约 §4 的最小闭环）。

本批约定（契约 §4 只固定了 ``data-dir`` 与 ``{"data": ...}`` 包装，下面几条是本实现的约定）：

- 上传体是 **JSON base64**，不引入 ``python-multipart``：客户端只要发
  ``{"name", "mime_type", "content_base64"}``。
- 磁盘布局 ``<data-dir>/media/<media_id>/<safe_name>``：一个媒体一个目录，删除即删目录，
  同名上传互不覆盖。
- 文件名一律过 :func:`safe_name`（只留 basename、替换保留字符），读路径一律过
  :func:`resolve_media_path`（``resolve()`` + ``is_relative_to`` 复核），任何请求都读不到
  ``media/`` 根之外的东西 —— **不接受客户端传路径**。
- 落盘走「同目录临时文件 + ``os.replace``」：半截文件不会以正式名字出现。
- 建表用 ``CREATE TABLE IF NOT EXISTS``，每次请求前跑一遍（本地单用户、语句幂等且便宜）；
  等第二批把仓库 ``migrations/`` 接进本地库之后，再收敛到 alembic 里。
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import asyncpg

from app.config import Settings

# 单个媒体上限 25 MiB。base64 膨胀约 4/3，因此 JSON 体上限约 33 MiB（uvicorn 默认不限，够用）。
MAX_MEDIA_BYTES = 25 * 1024 * 1024
MAX_NAME_CHARS = 100
DEFAULT_MIME_TYPE = "application/octet-stream"
MEDIA_DIR_NAME = "media"

# 媒体 id 同时也是目录名，必须是自证安全的标识符（uuid4 的 hex）
MEDIA_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_MIME_PATTERN = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")
_UNSAFE_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_NAME_SPLIT = re.compile(r"[\\/]")

MEDIA_COLUMNS = "id, name, safe_name, mime_type, size_bytes, sha256, created_at"
# INSERT 不能带 created_at：它由 DDL 的 DEFAULT now() 生成，显式列清单必须与 $n 个数一致
MEDIA_INSERT_COLUMNS = "id, name, safe_name, mime_type, size_bytes, sha256"

MEDIA_DDL = """
CREATE TABLE IF NOT EXISTS local_media (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    safe_name   TEXT NOT NULL,
    mime_type   TEXT NOT NULL,
    size_bytes  BIGINT NOT NULL,
    sha256      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MediaError(ValueError):
    """媒体输入不合法（名字 / base64 / 大小）。``status_code`` 由路由层直接使用。"""

    status_code = 400


class MediaTooLargeError(MediaError):
    """解码后超过 :data:`MAX_MEDIA_BYTES`。"""

    status_code = 413


class MediaStorageUnavailableError(MediaError):
    """本地 PostgreSQL 连不上（路由层映射 503，而不是 500）。"""

    status_code = 503


# --------------------------------------------------------------------- 命名与 MIME
def safe_name(raw: str) -> str:
    """把客户端给的文件名收敛成一个安全的 basename。

    ``"../../etc/passwd"`` → ``"passwd"``；``"a:b?.png"`` → ``"a_b_.png"``。
    清洗是**幂等**的：DB 里存的 ``safe_name`` 再过一遍结果不变。
    """
    name = (raw or "").strip()
    if not name:
        raise MediaError("name 不能为空")
    name = _NAME_SPLIT.split(name)[-1]  # 只留最后一段，挡掉 "../" 与 "C:\\..."
    name = _UNSAFE_NAME_CHARS.sub("_", name)
    name = name.strip().strip(".")  # 挡掉 "." / ".." / 隐藏文件
    if not name:
        raise MediaError(f"文件名不可用：{raw!r}")
    if len(name) > MAX_NAME_CHARS:
        stem, dot, suffix = name.rpartition(".")
        if dot and 0 < len(suffix) <= 16:
            name = f"{stem[: MAX_NAME_CHARS - len(suffix) - 1]}.{suffix}"
        else:
            name = name[:MAX_NAME_CHARS]
    return name


def normalize_mime_type(raw: str | None) -> str:
    """规整 MIME：小写、去掉 ``; charset=...``；认不出就退回 ``application/octet-stream``。"""
    value = (raw or "").split(";", 1)[0].strip().lower()
    return value if _MIME_PATTERN.fullmatch(value) else DEFAULT_MIME_TYPE


# --------------------------------------------------------------------- 路径
def media_root(settings: Settings) -> Path:
    """媒体根目录 ``<data-dir>/media``（只在这里拼，别的地方不要自己拼）。"""
    return settings.data_dir / MEDIA_DIR_NAME


def new_media_id() -> str:
    return uuid.uuid4().hex


def resolve_media_path(settings: Settings, media_id: str, name: str) -> Path:
    """``<media>/<id>/<safe_name>`` 的绝对路径，并确认没跑出 ``media/`` 根。"""
    if not MEDIA_ID_PATTERN.fullmatch(media_id or ""):
        raise MediaError(f"媒体 id 不合法：{media_id!r}")
    root = media_root(settings).resolve()
    candidate = (root / media_id / safe_name(name)).resolve()
    # 双保险：既要求字面上的「正好两层」，也要求最终路径落在根之内
    if not candidate.is_relative_to(root) or candidate.parent.parent != root:
        raise MediaError("媒体路径越界")
    return candidate


def decode_base64(content_base64: str, max_bytes: int | None = None) -> bytes:
    """解 JSON 里的 ``content_base64``（容忍 data URL 前缀与换行）。"""
    limit = MAX_MEDIA_BYTES if max_bytes is None else max_bytes
    if not isinstance(content_base64, str):
        raise MediaError("content_base64 必须是字符串")
    payload = content_base64.strip()
    if not payload:
        raise MediaError("content_base64 不能为空")
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    payload = "".join(payload.split())
    payload += "=" * (-len(payload) % 4)  # 浏览器侧常省略 padding
    try:
        data = base64.b64decode(payload, validate=True)
    except ValueError as exc:  # binascii.Error 是 ValueError 的子类
        raise MediaError(f"content_base64 不是合法 base64：{exc}") from exc
    if not data:
        raise MediaError("解码后内容为空")
    if len(data) > limit:
        raise MediaTooLargeError(f"文件过大：{len(data)} 字节 > 上限 {limit} 字节")
    return data


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------- 文件读写
def write_media_bytes(settings: Settings, media_id: str, name: str, data: bytes) -> Path:
    """原子落盘，返回最终路径。失败时不会留下半截的正式文件。"""
    target = resolve_media_path(settings, media_id, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.part")
    try:
        with staging.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, target)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    return target


def read_media_bytes(settings: Settings, media_id: str, name: str) -> bytes:
    return resolve_media_path(settings, media_id, name).read_bytes()


def delete_media_dir(settings: Settings, media_id: str) -> None:
    """删掉某个媒体的整个目录（id 不合法就当没有，什么都不删）。"""
    if not MEDIA_ID_PATTERN.fullmatch(media_id or ""):
        return
    root = media_root(settings).resolve()
    target = (root / media_id).resolve()
    if target.parent != root:
        return
    shutil.rmtree(target, ignore_errors=True)


# --------------------------------------------------------------------- 数据库
async def connect(dsn: str) -> asyncpg.Connection:
    """建一条 asyncpg 连接（本地单用户，本批不做连接池）。"""
    try:
        return await asyncpg.connect(dsn=dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        raise MediaStorageUnavailableError(
            f"本地 PostgreSQL 不可用（{type(exc).__name__}: {exc}）；检查 /readyz 的 pg 字段"
        ) from exc


async def ensure_media_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(MEDIA_DDL)


def row_to_dict(row: Any) -> dict[str, Any]:
    """asyncpg ``Record`` → ``dict``。

    注意：``Record`` 迭代产出的是**值**，所以 ``dict(record)`` 不成立，必须按 ``keys()`` 取。
    """
    return {key: row[key] for key in row.keys()}


async def insert_media(
    conn: asyncpg.Connection,
    *,
    media_id: str,
    name: str,
    safe_name: str,
    mime_type: str,
    size_bytes: int,
    sha256: str,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        # 列名是本模块常量，不是用户输入；值全部走 $n 参数化
        f"INSERT INTO local_media ({MEDIA_INSERT_COLUMNS}) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        f"RETURNING {MEDIA_COLUMNS}",
        media_id,
        name,
        safe_name,
        mime_type,
        size_bytes,
        sha256,
    )
    if row is None:
        raise RuntimeError("INSERT INTO local_media ... RETURNING 没有返回行")
    return row_to_dict(row)


async def get_media_row(conn: asyncpg.Connection, media_id: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        f"SELECT {MEDIA_COLUMNS} FROM local_media WHERE id = $1",
        media_id,
    )
    return None if row is None else row_to_dict(row)


async def list_media_rows(conn: asyncpg.Connection, *, limit: int, offset: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {MEDIA_COLUMNS} FROM local_media "
        "ORDER BY created_at DESC, id DESC LIMIT $1 OFFSET $2",
        limit,
        offset,
    )
    return [row_to_dict(row) for row in rows]


async def delete_media_row(conn: asyncpg.Connection, media_id: str) -> dict[str, Any] | None:
    """删记录并返回 ``{"safe_name": ...}``（拿它去删文件）；不存在返回 ``None``。"""
    row = await conn.fetchrow(
        "DELETE FROM local_media WHERE id = $1 RETURNING safe_name",
        media_id,
    )
    return None if row is None else row_to_dict(row)


# --------------------------------------------------------------------- 对外形状
def serialize_media(row: dict[str, Any]) -> dict[str, Any]:
    """对外形状：**不含** ``safe_name``，也**不含**任何本地绝对路径。"""
    media_id = str(row["id"])
    return {
        "id": media_id,
        "name": str(row["name"]),
        "mime_type": str(row["mime_type"]),
        "size_bytes": int(row["size_bytes"]),
        "sha256": str(row["sha256"]),
        "created_at": isoformat(row.get("created_at")),
        "content_url": f"/v1/media/{media_id}/content",
    }


def isoformat(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")
