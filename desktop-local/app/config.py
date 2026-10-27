"""本地 core 配置（契约 §4：data-dir / port / inference-proxy / db、redis 开关）。

所有字段都可用环境变量覆盖，前缀 ``CHIRON_LOCAL_``：

===============================  ============================================================
环境变量                         含义
===============================  ============================================================
``CHIRON_LOCAL_DATA_DIR``        数据目录（默认按平台选 userData 同级目录）
``CHIRON_LOCAL_PORT``            监听端口；``0`` = 由内核随机分配（默认）
``CHIRON_LOCAL_HOST``            监听地址，默认 ``127.0.0.1``（只绑 loopback，契约要求）
``CHIRON_LOCAL_INFERENCE_PROXY`` loopback 推理代理地址；空 = 未配置（W4 负责提供）
``CHIRON_LOCAL_PG_ENABLED``      是否启动 embedded PostgreSQL
``CHIRON_LOCAL_REDIS_ENABLED``   是否启动 Redis 适配层
``CHIRON_LOCAL_REDIS_URL``       给了就走真实 Redis/Valkey，否则用 fakeredis
``POSTGRES_DSN``                 给了就复用外部 PG（不自己起进程），优先级最高
``CHIRON_LOCAL_RUN_MIGRATIONS``  启动时是否跑 ``alembic upgrade head``
``CHIRON_LOCAL_AUTH_ENABLED``    是否校验设备 token（Bearer）；默认关闭
===============================  ============================================================
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "CHIRON_LOCAL_"

# desktop-local/app/config.py → parents[2] 即仓库根（复用仓库根的 alembic.ini 与 migrations/）
REPO_ROOT = Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    """按平台给出默认数据目录。

    Electron 侧会用 ``userData/local`` 覆盖它（契约 §1），这里只是让 CLI 单独跑时也有个
    可写且不污染仓库的位置。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Chiron" / "local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Chiron" / "local"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "chiron" / "local"


class Settings(BaseSettings):
    """本地 core 的运行期配置。"""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # POSTGRES_DSN 没有前缀，且允许按字段名传参（Settings(postgres_dsn=...)）
        populate_by_name=True,
    )

    # --- 进程 ---
    host: str = "127.0.0.1"
    port: int = 0  # 0 = 随机端口
    log_level: str = "info"
    data_dir: Path = Field(default_factory=default_data_dir)

    # --- 推理代理（W4 提供，本批只做配置占位）---
    inference_proxy: str = ""

    # --- 存储开关 ---
    pg_enabled: bool = True
    redis_enabled: bool = True
    run_migrations: bool = True
    postgres_dsn: str | None = Field(
        default=None,
        validation_alias=AliasChoices("POSTGRES_DSN", f"{ENV_PREFIX}POSTGRES_DSN"),
    )
    redis_url: str = ""

    # --- embedded PostgreSQL ---
    pg_database: str = "chiron"
    pg_user: str = "chiron"
    pg_password: str = "chiron"  # 仅用于本机 trust/loopback 场景，不落到任何日志
    pg_port: int = 0  # 0 = 每次启动选一个空闲端口
    pg_bin_dir: str = ""  # 指向 initdb/pg_ctl 所在目录（找不到二进制时的兜底）
    pg_start_timeout: float = 60.0

    # --- SSE ---
    sse_heartbeat_seconds: float = 15.0
    sse_max_heartbeats: int = 0  # 0 = 不设上限（测试用 1~2 让它自然结束）

    # --- 鉴权 ---
    # 默认关闭：能连上 loopback 就已经是本机主人，加口令只是多一个失败点（契约 §4）。
    # 置 ``CHIRON_LOCAL_AUTH_ENABLED=1`` 后由 DeviceTokenMiddleware 校验 Bearer 设备 token。
    auth_enabled: bool = False

    @field_validator("port", "pg_port")
    @classmethod
    def _check_port(cls, value: int) -> int:
        if value < 0 or value > 65535:
            raise ValueError("端口必须在 0..65535 之间（0 = 随机端口）")
        return value

    @field_validator("data_dir")
    @classmethod
    def _expand_data_dir(cls, value: Path) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()

    # --- 派生路径 ---
    @property
    def pg_data_dir(self) -> Path:
        """embedded PG 的数据目录：``<data-dir>/pg``（契约要求）。"""
        return self.data_dir / "pg"

    @property
    def pg_log_file(self) -> Path:
        return self.data_dir / "logs" / "postgres.log"

    @property
    def auth_dir(self) -> Path:
        return self.data_dir / "auth"

    @property
    def device_token_path(self) -> Path:
        return self.auth_dir / "device-token"

    @property
    def migrations_dir(self) -> Path:
        return REPO_ROOT / "migrations"

    @property
    def alembic_ini(self) -> Path:
        return REPO_ROOT / "alembic.ini"

    def ensure_dirs(self) -> None:
        """建好本进程需要写的目录（只写 data-dir 之下）。"""
        for path in (self.data_dir, self.pg_data_dir.parent, self.pg_log_file.parent, self.auth_dir):
            path.mkdir(parents=True, exist_ok=True)

    def auth_mode(self) -> str:
        """``/readyz`` 的 ``auth`` 字段：开着报具体方式，关着报 ``disabled``。"""
        return "device_token" if self.auth_enabled else "disabled"

    def describe(self) -> dict[str, object]:
        """给 ``/readyz`` 与日志用的、**不含密钥**的配置快照。"""
        return {
            "data_dir": str(self.data_dir),
            "host": self.host,
            "port": self.port,
            "inference_proxy": self.inference_proxy,
            "pg_enabled": self.pg_enabled,
            "redis_enabled": self.redis_enabled,
            "redis_url": self.redis_url,
            "run_migrations": self.run_migrations,
            "auth": self.auth_mode(),
        }
