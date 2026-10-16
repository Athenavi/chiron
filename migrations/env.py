"""
Alembic env configuration.
Resolves the database URL from:
  1. DATABASE_DSN environment variable
  2. alembic.ini fallback
"""
import os
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 加载 .env 文件 - 支持多个位置
project_root = Path(__file__).parent.parent

# 按优先级尝试加载 .env 文件
env_candidates = [
    project_root / '.env',              # 根目录 .env
    project_root / 'config' / '.env',   # config 目录 .env
]
env_loaded = False
for env_file in env_candidates:
    if env_file.exists():
        load_dotenv(env_file, override=True)
        print(f"[Alembic] Loaded .env from {env_file}")
        env_loaded = True
        break

if not env_loaded:
    print(f"[Alembic] Warning: .env file not found. Searched:")
    for p in env_candidates:
        print(f"  - {p}")

def get_database_url() -> str | None:
    """Resolve database URL from DATABASE_DSN env → alembic.ini fallback."""
    # 1) DATABASE_DSN environment variable (highest priority)
    db_dsn = os.getenv("DATABASE_DSN")
    if db_dsn:
        return db_dsn

    # 2) alembic.ini fallback
    fallback = config.get_main_option("sqlalchemy.url")
    if fallback:
        print(f"[Alembic] Using fallback URL from alembic.ini")
        return fallback

    return None

# Set the database URL
db_url = get_database_url()
# 隐藏密码打印
# 隐藏密码打印：密码可能含 @ 与 :（如 p@ssw0rd!），必须按「最后一个 @」定位 host，
# 否则密码后半段会被当成 host 打印进 Alembic 日志（凭据泄露）。
safe_url = "(none)"
if db_url:
    if '@' in db_url:
        _scheme, _, _rest = db_url.partition('://')
        _userinfo, _, _host = _rest.rpartition('@')
        if ':' in _userinfo:
            safe_url = f"{_scheme}://{_userinfo.split(':', 1)[0]}:***@{_host}"
        else:
            safe_url = db_url
    else:
        safe_url = db_url
print(f"[Alembic] Database URL: {safe_url}")
if db_url:
    # Ensure postgresql:// prefix for SQLAlchemy
    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql://', 1)
    
    # URL-encode the password if it contains special characters
    # Use regex to extract and re-encode the password portion
    from urllib.parse import quote
    import re
    
    try:
        # Match pattern: postgresql://user:password@host...
        # Use greedy match for password to handle @ in password
        # The last @ before host is the separator
        match = re.match(r'(postgresql://[^:]+:)(.+)(@[^@]+:\d+/.+)', db_url)
        if match:
            prefix = match.group(1)  # postgresql://user:
            raw_password = match.group(2)  # raw password (may contain @ and other special chars)
            suffix = match.group(3)  # @host:port/db?params
            
            # URL-encode the password
            from urllib.parse import quote
            encoded_password = quote(raw_password, safe='')
            db_url = prefix + encoded_password + suffix
            print(f"[Alembic] Password URL-encoded successfully")
        else:
            # Try simpler pattern without port
            match = re.match(r'(postgresql://[^:]+:)(.+)(@[^@:]+\..+)', db_url)
            if match:
                prefix = match.group(1)
                raw_password = match.group(2)
                suffix = match.group(3)
                
                from urllib.parse import quote
                encoded_password = quote(raw_password, safe='')
                db_url = prefix + encoded_password + suffix
                print(f"[Alembic] Password URL-encoded successfully (simple pattern)")
            else:
                print(f"[Alembic] Warning: Could not parse URL structure for password encoding")
                print(f"[Alembic] Debug: db_url = {db_url}")
    except Exception as e:
        print(f"[Alembic] Warning: Failed to encode URL: {e}")
    
    config.set_main_option("sqlalchemy.url", db_url)

# 迁移为**手写 SQL**（各迁移文件的 op.execute）：这里不再注册 ORM metadata。
# 2026-10-16：`models.yaml → shared/models ORM → 守卫` 的生成链路已整体移除
# （快速迭代期直接改数据库；configs/orm/V1/models.yaml 仅作 CI/CD 与 API 参考）。
# autogenerate 因此不再可用（也不需要）—— upgrade/downgrade 只按
# migrations/versions/*.py 里写的 SQL 执行。
target_metadata = None

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,  # 比较列类型
            render_as_batch=True,  # 支持批量操作（SQLite需要）
            include_schemas=True,  # 包含所有schema
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
