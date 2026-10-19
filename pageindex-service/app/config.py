"""PageIndex 服务/worker 的运行时配置（全部来自环境变量，且**必须显式**）。

与服务共享同一份配置的原因：两者在同一镜像、同一依赖环境里，读的是同一组部署变量
（``KNOWLEDGE_*`` / ``S3_*`` / ``REDIS_URL`` / ``POSTGRES_DSN``）。分成两份只会漂移。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    # 用 `or` 而不是 getenv 的默认值：compose 的 `${VAR:-}` 注入的是**空串**（键存在），
    # 而 getenv 的 default 只在键不存在时生效 —— 空串会把回退链悄悄吃掉。
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # ── 服务本身 ──
    internal_token: str
    port: int

    # ── 队列（worker 消费 engine:tasks:kb）──
    redis_url: str
    kb_stream: str
    kb_group: str
    kb_dlq: str
    kb_delayed: str
    kb_stream_maxlen: int
    worker_concurrency: int

    # ── 数据库（worker 回写索引状态）──
    postgres_dsn: str

    # ── 原文对象存储（worker 读取原文才能索引）──
    blob_backend: str
    blob_local_path: str
    s3_bucket: str
    s3_endpoint_url: str
    s3_endpoint_host: str
    s3_use_ssl: bool
    s3_access_key: str
    s3_secret_key: str
    s3_prefix: str

    # ── 自托管索引（local 模式）──
    selfhosted_root: str
    selfhosted_index_model: str
    selfhosted_chat_model: str

    # ── Cloud 模式的问答模型 ──
    cloud_chat_model: str

    # ── 自托管的 LLM 连接（**索引侧独立配置**：与引擎的 provider 体系解耦）──
    # 留空 = 交给 litellm 读它的标准环境变量（OPENAI_API_KEY 等）。企业部署下这样更好：
    # 索引组件的凭据与在线对话的凭据可以分开轮换。
    selfhosted_llm_base_url: str
    selfhosted_llm_api_key: str
    # 0.4.4 起 optimize 默认 "full"（加一轮 LLM expand）：显式配置，免得跟着默认值漂移
    selfhosted_optimize: str

    # ── 用户级 Cloud key 的解密（与引擎 internal/settings 同算法）──
    app_secret: str

    def llm_backend(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.selfhosted_llm_api_key:
            out["api_key"] = self.selfhosted_llm_api_key
        if self.selfhosted_llm_base_url:
            out["base_url"] = self.selfhosted_llm_base_url
        return out

    @classmethod
    def from_env(cls) -> "Config":
        from app.runtime import rkey

        return cls(
            internal_token=_env("INTERNAL_TOKEN"),
            port=_env_int("PAGEINDEX_PORT", 8100),
            redis_url=_env("REDIS_URL"),
            kb_stream=rkey("engine:tasks:kb"),
            kb_group="kb-workers",
            kb_dlq=rkey("engine:tasks:kb:dlq"),
            kb_delayed=rkey("engine:tasks:kb:delayed"),
            kb_stream_maxlen=100000,
            worker_concurrency=_env_int("PAGEINDEX_WORKER_CONCURRENCY", 2),
            postgres_dsn=_env("POSTGRES_DSN"),
            # 与引擎同一套口径：未配 KNOWLEDGE_* 时跟随 MEDIA_STORE_BACKEND
            blob_backend=(_env("KNOWLEDGE_BLOB_BACKEND") or _env("MEDIA_STORE_BACKEND") or "local").lower(),
            blob_local_path=_env("KNOWLEDGE_LOCAL_PATH", "/srv/chiron/knowledge"),
            s3_bucket=_env("KNOWLEDGE_S3_BUCKET") or _env("S3_BUCKET"),
            s3_endpoint_url=_env("KNOWLEDGE_S3_ENDPOINT_URL") or _env("S3_ENDPOINT_URL"),
            # 与引擎同一口径：.env 里常见的是**纯 host**（S3_ENDPOINT_HOST）+ 是否 TLS；
            # 之前只认 S3_ENDPOINT_URL，而 .env 里根本没有这个名字 —— worker 会静默连 AWS 默认端点。
            s3_endpoint_host=_env("KNOWLEDGE_S3_ENDPOINT_HOST") or _env("S3_ENDPOINT_HOST"),
            s3_use_ssl=(_env("S3_USE_SSL", "true").lower() not in ("0", "false", "no")),
            s3_access_key=_env("KNOWLEDGE_S3_ACCESS_KEY") or _env("S3_ACCESS_KEY"),
            s3_secret_key=_env("KNOWLEDGE_S3_SECRET_KEY") or _env("S3_SECRET_KEY"),
            s3_prefix=_env("KNOWLEDGE_S3_PREFIX", "knowledge/"),
            selfhosted_root=_env("KNOWLEDGE_SELFHOSTED_ROOT", "/srv/chiron/knowledge_index"),
            selfhosted_index_model=_env("KNOWLEDGE_SELFHOSTED_INDEX_MODEL"),
            selfhosted_chat_model=_env("KNOWLEDGE_SELFHOSTED_CHAT_MODEL"),
            cloud_chat_model=_env("KNOWLEDGE_CLOUD_CHAT_MODEL"),
            selfhosted_llm_base_url=_env("KNOWLEDGE_SELFHOSTED_LLM_BASE_URL"),
            selfhosted_llm_api_key=_env("KNOWLEDGE_SELFHOSTED_LLM_API_KEY"),
            selfhosted_optimize=_env("KNOWLEDGE_SELFHOSTED_OPTIMIZE", "full"),
            app_secret=_env("APP_SECRET"),
        )

    def selfhosted_ready(self) -> bool:
        return bool(self.selfhosted_root and self.selfhosted_index_model and self.selfhosted_chat_model)

    def cloud_ready(self) -> bool:
        return bool(self.cloud_chat_model)
