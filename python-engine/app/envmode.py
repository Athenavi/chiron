"""部署环境判定（生产 / 非生产）—— **全仓唯一入口**。

为什么值得单独立一个模块：此前有两套判定，各读各的变量 ——

    app/config.py     看 APP_ENV / PYTHON_ENV   → 管 POSTGRES_DSN 必填、禁止 sslmode=disable
    app/deps.py       看 CHIRON_ENV             → 管 SANDBOX_ROOT 必须指向共享卷

于是运维**只设其中一个**时，另一半门禁**静默失效**：只设 CHIRON_ENV 时，
"生产必须显式配 DSN / 禁用 sslmode=disable"不会触发；只设 APP_ENV 时，
"SANDBOX_ROOT 必须共享卷"不会触发。两条都属于"配置错了却照样起得来"的类型 ——
正是 fail-fast 最该拦下的场景。

现在两处都走本模块。取值口径是**并集**而不是改名：`CHIRON_ENV` / `APP_ENV` /
`PYTHON_ENV` 任一为 `production` / `prod` 即视为生产。这样既有部署无论当初设的是
哪一个，行为都只会变得更严格、不会松动 —— 不需要运维改配置，也不会因为漏设某个
变量而门禁失效。

Go 侧不参与本判定：`config.ValidateJWTSecret` 对弱密钥的拒绝是**无条件**的
（不依赖环境变量），这里不需要对称实现。
"""

from __future__ import annotations

import os

#: 被认作"环境名"的变量。`CHIRON_ENV` 是本仓主名，另两个是既有部署/生态习惯。
ENV_VARS: tuple[str, ...] = ("CHIRON_ENV", "APP_ENV", "PYTHON_ENV")

#: 视为生产的取值（大小写与首尾空白不敏感）。
_PRODUCTION_NAMES = frozenset({"production", "prod"})


def env_name() -> str:
    """第一个被设置的环境名变量取值（小写）；都没设时返回空串。

    只用于日志与错误文案 —— 判定请用 :func:`is_production`。
    """
    for var in ENV_VARS:
        value = (os.getenv(var) or "").strip()
        if value:
            return value.lower()
    return ""


def is_production() -> bool:
    """任一环境变量命中生产名即 True。

    刻意用并集：宁可多判一次生产（多要一个配置项），也不要漏判
    —— 漏判的代价是"生产环境带着开发默认值跑起来"。
    """
    return any(
        (os.getenv(var) or "").strip().lower() in _PRODUCTION_NAMES for var in ENV_VARS
    )
