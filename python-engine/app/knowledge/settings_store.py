"""用户级知识库设置：provider 选择与 Cloud key 密文。

**一个用户只能选一种 provider**（产品约束）：选 Cloud 必须配 key，选自托管则清掉 Cloud key。
不做"两个都留着、运行时再挑" —— 那样"为什么这次检索走了云"就没有答案。

密文复用 ``internal/settings`` 的 AEAD（见 ``app/knowledge/crypto.py``）；``APP_SECRET``
未配置时**拒绝保存密钥**，而不是用一把空密钥写进一份永远解不开的密文。

表有 ``user_id`` 主键（0023），所以写入用 ``ON CONFLICT`` 原子 upsert —— 这正是旧
``system_settings`` 做不到的（那张表没有唯一约束，并发写会各自成功并互相覆盖）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.knowledge import crypto
from app.knowledge.errors import KnowledgeError, ProviderUnavailable
from app.knowledge.providers.base import (
    PROVIDER_CLOUD,
    STATUS_DISABLED,
    STATUS_ERROR,
    is_known_provider,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserKnowledgeSettings:
    user_id: str
    tenant_id: str
    provider: str
    cloud_api_key: str = ""          # 解密后的明文，仅存在于内存
    cloud_folder_id: str = ""        # PageIndex Cloud 的用户级 folder（非安全边界，见迁移 0024）
    status: str = STATUS_DISABLED
    error_message: str = ""


async def _pool() -> Any:
    from app.db import get_pool

    return get_pool()


async def load(user_id: str) -> UserKnowledgeSettings | None:
    """读取并解密用户设置；密文解不开时返回 status=error（不抛异常）。"""
    if not user_id:
        return None
    pool = await _pool()
    row = await pool.fetchrow(
        """SELECT user_id, tenant_id, provider, cloud_api_key_enc, cloud_folder_id,
                  status, error_message
             FROM user_knowledge_settings WHERE user_id = $1""",
        user_id,
    )
    if row is None:
        return None

    encrypted = row["cloud_api_key_enc"] or ""
    api_key = ""
    status = str(row["status"] or STATUS_DISABLED)
    error_message = str(row["error_message"] or "")
    if encrypted:
        try:
            api_key = crypto.decrypt_secret(encrypted, settings.app_secret)
        except crypto.SecretCryptoError:
            # 主密钥轮换后旧密文解不开：这是**配置事故**，必须让它可见（状态标 error），
            # 而不是静默当成"没配 key"
            status = STATUS_ERROR
            error_message = "Cloud API key 无法解密（APP_SECRET 可能已变更），请重新填写"
            logger.warning("knowledge: cannot decrypt cloud key for user=%s", user_id)

    return UserKnowledgeSettings(
        user_id=str(row["user_id"]),
        tenant_id=str(row["tenant_id"] or ""),
        provider=str(row["provider"] or ""),
        cloud_api_key=api_key,
        cloud_folder_id=str(row["cloud_folder_id"] or ""),
        status=status,
        error_message=error_message,
    )


async def save(
    *,
    user_id: str,
    tenant_id: str,
    provider: str,
    cloud_api_key: str | None = None,
) -> UserKnowledgeSettings:
    """保存 provider 选择（Cloud 时同时写入加密后的 key）。

    ``cloud_api_key`` 为 None 表示"沿用已存的 key"（改选 provider 之外的场景不强迫重填）；
    显式传空串表示"清空 key"。
    """
    if not user_id:
        raise KnowledgeError("user_id is required")
    if not is_known_provider(provider):
        raise KnowledgeError(f"unknown knowledge provider: {provider!r}")

    status = STATUS_DISABLED
    encrypted = ""
    fingerprint = ""

    if provider == PROVIDER_CLOUD:
        existing = await load(user_id)
        key = cloud_api_key if cloud_api_key is not None else (existing.cloud_api_key if existing else "")
        key = (key or "").strip()
        if not key:
            raise KnowledgeError("选择 Cloud 模式必须提供 PageIndex Cloud API key")
        if not crypto.encryption_available(settings.app_secret):
            raise ProviderUnavailable("APP_SECRET 未配置，无法保存 Cloud API key")
        encrypted = crypto.encrypt_secret(key, settings.app_secret)
        fingerprint = crypto.key_fingerprint(key)
        status = "active"
    # 自托管：清空 key（二选一），状态留给健康检查决定（disabled/ready/error）

    pool = await _pool()
    await pool.execute(
        """INSERT INTO user_knowledge_settings
               (user_id, tenant_id, provider, cloud_api_key_enc, cloud_key_hash, status, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, NOW())
           ON CONFLICT (user_id) DO UPDATE
              SET tenant_id = EXCLUDED.tenant_id,
                  provider = EXCLUDED.provider,
                  cloud_api_key_enc = EXCLUDED.cloud_api_key_enc,
                  cloud_key_hash = EXCLUDED.cloud_key_hash,
                  status = EXCLUDED.status,
                  error_message = NULL,
                  -- folder 属于"那个账号"的空间：换了 provider/key 就必须重新建
                  cloud_folder_id = CASE
                      WHEN user_knowledge_settings.provider IS DISTINCT FROM EXCLUDED.provider
                          THEN NULL
                      ELSE user_knowledge_settings.cloud_folder_id
                  END,
                  updated_at = NOW()""",
        user_id,
        tenant_id or "",
        provider,
        encrypted or None,
        fingerprint or None,
        status,
    )
    logger.info("knowledge: user=%s provider=%s saved", user_id, provider)
    saved = await load(user_id)
    assert saved is not None  # 刚 upsert，必然读得到
    return saved


async def update_status(*, user_id: str, status: str, error_message: str = "") -> None:
    """回写探测结果（"测试连接"或后台巡检）。"""
    if not user_id:
        return
    pool = await _pool()
    await pool.execute(
        """UPDATE user_knowledge_settings
              SET status = $2, error_message = $3, last_checked_at = NOW(), updated_at = NOW()
            WHERE user_id = $1""",
        user_id,
        status,
        error_message or None,
    )


async def set_cloud_folder(*, user_id: str, folder_id: str) -> None:
    """记录用户的 Cloud 顶层 folder（首次索引建好后回写，之后不必再列取）。

    只写这一列：folder 与 key 属于同一个账号空间，改 key/provider 时由 ``save`` 清空，
    这里不做任何推断。
    """
    if not user_id or not folder_id:
        return
    pool = await _pool()
    await pool.execute(
        """UPDATE user_knowledge_settings
              SET cloud_folder_id = $2, updated_at = NOW()
            WHERE user_id = $1""",
        user_id,
        folder_id,
    )
