"""部署级主密钥（APP_SECRET）与派生 JWT_SECRET 的安全校验。

回归背景：引擎的校验点原本**只有派生后的 jwt_secret**。派生结果是 43 字符的
base64url 串，永远不在弱值黑名单里 —— 于是 `.env.example` 里那把「全仓公开」的
示例 APP_SECRET 能一路通过校验启动，用它签发的 token 谁都能伪造。

Go 侧（config/config.go::ValidateJWTSecret）已把该示例值列入 weakSecrets，
这里断言 Python 侧同口径。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

#: `.env.example` 历史上的示例值：≥32 字符，因此绕过了长度门槛。
PUBLIC_EXAMPLE = "your-32-plus-char-random-secret-generate-with-secrets-token-urlsafe"

#: 与 Go 侧 weakSecrets 同集合。
WEAK_SECRETS = [
    PUBLIC_EXAMPLE,
    "dev-secret-change-in-production",
    "dev-secret-change-in-production-12345678",
    "changeme",
    "change-me",
    "secret",
    "test-secret",
]


@pytest.mark.parametrize("weak", WEAK_SECRETS)
def test_weak_app_secret_is_rejected(weak: str) -> None:
    """公开/弱示例 APP_SECRET 必须拒绝启动，哪怕是「只配 APP_SECRET」的部署模型。"""
    with pytest.raises(ValidationError) as exc:
        Settings(app_secret=weak, jwt_secret="", internal_token="", _env_file=None)
    assert "APP_SECRET" in str(exc.value)


def test_strong_app_secret_is_accepted() -> None:
    """正常生成的随机串必须照常通过，否则合法部署会被误伤。"""
    cfg = Settings(
        app_secret="q3F9vT2mKx7LpR4sW8nZ1cY6hB0dJ5gA",
        jwt_secret="",
        internal_token="",
        _env_file=None,
    )
    assert cfg.app_secret
    # 只配 APP_SECRET 时应派生出一个可用的 jwt_secret（与 Go 网关同口径）
    assert len(cfg.jwt_secret) == 43
    assert len(cfg.internal_token) == 43


def test_derived_jwt_secret_still_validated() -> None:
    """显式给的 JWT_SECRET 仍要过硬值校验 —— 派生路径不能成为绕过口。"""
    with pytest.raises(ValidationError):
        Settings(app_secret="", jwt_secret="short", internal_token="", _env_file=None)
