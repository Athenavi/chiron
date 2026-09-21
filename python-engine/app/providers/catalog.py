"""服务提供商目录（引擎侧）。

权威源在 Go 网关 ``internal/api/llm_providers.go``：网关通过内部端点
``GET /v1/internal/engine-config`` 的 ``llm_provider_catalog`` 字段下发目录，
本模块在此基础上提供

* 网关不可达（fail-open，引擎仍能启动）时的**兜底目录**；
* provider → 端点 / key 的**解析规则**（DB 覆盖 > env > Settings 字段 > 目录默认）。

新增提供商只需改网关目录（本文件的 ``FALLBACK_CATALOG`` 保同 id/kind 以便单机降级）。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# 兜底目录：与 internal/api/llm_providers.go 的 llmProviderCatalog 保持 id/kind/base_url 一致。
# 网关下发成功时以网关目录为准，本表仅补齐网关未列出的 id（如引擎比网关新的场景）。
FALLBACK_CATALOG: list[dict] = [
    # ── 国际厂商 ──
    {"id": "openai", "label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1",
     "api_key_env": "OPENAI_API_KEY", "model_prefixes": ["gpt", "o1", "o3", "o4", "davinci", "text-embedding"],
     "cost": 5.0, "quality": 0.90, "requires_key": True, "model_discovery": True},
    {"id": "anthropic", "label": "Anthropic Claude", "kind": "anthropic", "base_url": "https://api.anthropic.com",
     "api_key_env": "ANTHROPIC_API_KEY", "model_prefixes": ["claude"],
     "cost": 3.0, "quality": 0.95, "requires_key": True, "model_discovery": False},
    {"id": "google", "label": "Google Gemini", "kind": "openai",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "api_key_env": "GEMINI_API_KEY", "model_prefixes": ["gemini", "gemma"],
     "cost": 1.5, "quality": 0.90, "requires_key": True, "model_discovery": True},
    {"id": "xai", "label": "xAI Grok", "kind": "openai", "base_url": "https://api.x.ai/v1",
     "api_key_env": "XAI_API_KEY", "model_prefixes": ["grok"],
     "cost": 3.0, "quality": 0.88, "requires_key": True, "model_discovery": True},
    {"id": "groq", "label": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1",
     "api_key_env": "GROQ_API_KEY", "model_prefixes": ["llama", "mixtral", "gemma", "whisper"],
     "cost": 0.6, "quality": 0.82, "requires_key": True, "model_discovery": True},
    {"id": "mistral", "label": "Mistral AI", "kind": "openai", "base_url": "https://api.mistral.ai/v1",
     "api_key_env": "MISTRAL_API_KEY", "model_prefixes": ["mistral", "codestral", "ministral", "pixtral"],
     "cost": 1.0, "quality": 0.84, "requires_key": True, "model_discovery": True},
    {"id": "together", "label": "Together AI", "kind": "openai", "base_url": "https://api.together.xyz/v1",
     "api_key_env": "TOGETHER_API_KEY", "model_prefixes": [],
     "cost": 0.9, "quality": 0.82, "requires_key": True, "model_discovery": True},

    # ── 国内厂商 ──
    {"id": "deepseek", "label": "DeepSeek", "kind": "openai", "base_url": "https://api.deepseek.com",
     "api_key_env": "DEEPSEEK_API_KEY", "model_prefixes": ["deepseek"],
     "cost": 0.2, "quality": 0.80, "requires_key": True, "model_discovery": True},
    {"id": "moonshot", "label": "月之暗面 Kimi", "kind": "openai", "base_url": "https://api.moonshot.cn/v1",
     "api_key_env": "MOONSHOT_API_KEY", "model_prefixes": ["moonshot", "kimi"],
     "cost": 1.2, "quality": 0.85, "requires_key": True, "model_discovery": True},
    {"id": "zhipu", "label": "智谱 GLM", "kind": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "api_key_env": "ZHIPU_API_KEY", "model_prefixes": ["glm", "chatglm"],
     "cost": 0.6, "quality": 0.82, "requires_key": True, "model_discovery": True},
    {"id": "dashscope", "label": "阿里云通义千问", "kind": "openai",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "api_key_env": "DASHSCOPE_API_KEY", "model_prefixes": ["qwen", "qwq", "tongyi"],
     "cost": 0.8, "quality": 0.84, "requires_key": True, "model_discovery": True},
    {"id": "minimax", "label": "MiniMax", "kind": "openai", "base_url": "https://api.minimax.chat/v1",
     "api_key_env": "MINIMAX_API_KEY", "model_prefixes": ["minimax", "abab"],
     "cost": 1.0, "quality": 0.80, "requires_key": True, "model_discovery": True},
    {"id": "baichuan", "label": "百川智能", "kind": "openai", "base_url": "https://api.baichuan-ai.com/v1",
     "api_key_env": "BAICHUAN_API_KEY", "model_prefixes": ["baichuan"],
     "cost": 0.6, "quality": 0.78, "requires_key": True, "model_discovery": True},
    {"id": "hunyuan", "label": "腾讯混元", "kind": "openai",
     "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
     "api_key_env": "HUNYUAN_API_KEY", "model_prefixes": ["hunyuan"],
     "cost": 0.7, "quality": 0.80, "requires_key": True, "model_discovery": True},
    {"id": "stepfun", "label": "阶跃星辰 Step", "kind": "openai", "base_url": "https://api.stepfun.com/v1",
     "api_key_env": "STEPFUN_API_KEY", "model_prefixes": ["step"],
     "cost": 0.8, "quality": 0.80, "requires_key": True, "model_discovery": True},

    # ── 聚合网关 ──
    {"id": "openrouter", "label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1",
     "api_key_env": "OPENROUTER_API_KEY", "model_prefixes": ["openrouter/"],
     "cost": 5.0, "quality": 0.85, "requires_key": True, "model_discovery": True},
    {"id": "siliconflow", "label": "硅基流动 SiliconFlow", "kind": "openai",
     "base_url": "https://api.siliconflow.cn/v1",
     "api_key_env": "SILICONFLOW_API_KEY",
     "model_prefixes": ["Qwen/", "deepseek-ai/", "THUDM/", "Pro/"],
     "cost": 0.5, "quality": 0.80, "requires_key": True, "model_discovery": True},
    {"id": "oneapi", "label": "One API / New API 自建网关", "kind": "openai", "base_url": "",
     "api_key_env": "ONEAPI_API_KEY", "model_prefixes": [],
     "cost": 5.0, "quality": 0.80, "requires_key": True, "model_discovery": True},
    # OpenCode 官方网关（两条产品线：Zen 按量计费 / Go 订阅）。端点均支持 {base}/models
    # 自动发现；model_prefixes 留空，避免抢走其它直连 provider 的模型路由。
    {"id": "opencode", "label": "OpenCode Zen", "kind": "openai",
     "base_url": "https://opencode.ai/zen/v1",
     "api_key_env": "OPENCODE_API_KEY", "model_prefixes": [],
     "cost": 2.0, "quality": 0.88, "requires_key": True, "model_discovery": True},
    {"id": "opencode-anthropic", "label": "OpenCode Zen（Anthropic 协议）", "kind": "anthropic",
     "base_url": "https://opencode.ai/zen",
     "api_key_env": "OPENCODE_API_KEY", "model_prefixes": [],
     "cost": 2.0, "quality": 0.88, "requires_key": True, "model_discovery": False},
    {"id": "opencode-go", "label": "OpenCode Go（订阅）", "kind": "openai",
     "base_url": "https://opencode.ai/zen/go/v1",
     "api_key_env": "OPENCODE_GO_API_KEY", "model_prefixes": [],
     "cost": 0.4, "quality": 0.85, "requires_key": True, "model_discovery": True},
    {"id": "opencode-go-anthropic", "label": "OpenCode Go（Anthropic 协议）", "kind": "anthropic",
     "base_url": "https://opencode.ai/zen/go",
     "api_key_env": "OPENCODE_GO_API_KEY", "model_prefixes": [],
     "cost": 0.4, "quality": 0.85, "requires_key": True, "model_discovery": False},

    # ── 自托管推理 ──
    {"id": "ollama", "label": "Ollama（本地）", "kind": "openai", "base_url": "http://localhost:11434/v1",
     "api_key_env": "OLLAMA_API_KEY", "model_prefixes": [],
     "cost": 0.0, "quality": 0.60, "requires_key": False, "model_discovery": True},
    {"id": "vllm", "label": "vLLM（自托管）", "kind": "openai", "base_url": "http://localhost:8000/v1",
     "api_key_env": "VLLM_API_KEY", "model_prefixes": [],
     "cost": 0.0, "quality": 0.62, "requires_key": False, "model_discovery": True},
    {"id": "lmstudio", "label": "LM Studio（本地）", "kind": "openai", "base_url": "http://localhost:1234/v1",
     "api_key_env": "LMSTUDIO_API_KEY", "model_prefixes": [],
     "cost": 0.0, "quality": 0.58, "requires_key": False, "model_discovery": True},

    # ── 自定义 ──
    {"id": "custom", "label": "自定义（OpenAI 兼容）", "kind": "openai", "base_url": "",
     "api_key_env": "CUSTOM_API_KEY", "model_prefixes": [],
     "cost": 5.0, "quality": 0.80, "requires_key": True, "model_discovery": True},
    {"id": "custom-anthropic", "label": "自定义（Anthropic 协议）", "kind": "anthropic", "base_url": "",
     "api_key_env": "CUSTOM_ANTHROPIC_API_KEY", "model_prefixes": [],
     "cost": 3.0, "quality": 0.85, "requires_key": True, "model_discovery": False},
]


def _settings():
    """延迟导入 Settings，避免 config ←→ providers 的循环导入。"""
    from app.config import settings

    return settings


def provider_catalog() -> list[dict]:
    """返回生效的服务提供商目录（网关下发优先，兜底目录补齐缺失 id）。"""
    by_id: dict[str, dict] = {}
    settings = _settings()
    remote = getattr(settings, "llm_provider_catalog", None) or []
    for item in remote:
        if isinstance(item, dict) and item.get("id"):
            by_id[str(item["id"])] = dict(item)
    for item in FALLBACK_CATALOG:
        by_id.setdefault(item["id"], dict(item))
    return list(by_id.values())


def provider_overrides() -> dict[str, str]:
    """网关「系统设置」下发的 provider 级覆盖（``{provider}_base_url`` / ``_api_key``）。"""
    settings = _settings()
    raw = getattr(settings, "provider_overrides", None) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, (str, int, float)) and str(v)}


def _provider_env_name(provider_id: str, suffix: str) -> str:
    """``moonshot`` + ``_BASE_URL`` → ``MOONSHOT_BASE_URL``。"""
    return provider_id.upper().replace("-", "_").replace(".", "_") + suffix


def provider_base_url(preset: dict) -> str:
    """解析 provider 端点。优先级（降序）：
    管理端 DB 覆盖 → ``{PROVIDER}_BASE_URL`` env → Settings 的 ``{provider}_base_url``
    字段 → openai 的 ``LLM_BASE_URL`` 历史回退 → 目录默认端点。
    """
    pid = str(preset.get("id") or "")
    if not pid:
        return ""
    settings = _settings()

    override = provider_overrides().get(f"{pid}_base_url", "").strip()
    if override:
        return override.rstrip("/")

    env_value = os.getenv(_provider_env_name(pid, "_BASE_URL"), "").strip()
    if env_value:
        return env_value.rstrip("/")

    field_value = str(getattr(settings, f"{pid}_base_url", "") or "").strip()
    if field_value:
        return field_value.rstrip("/")

    # 历史行为：LLM_BASE_URL 作为 OpenAI 兼容统一端点（见 config._resolve_llm_fallback）
    if pid == "openai":
        legacy = str(getattr(settings, "llm_base_url", "") or "").strip()
        if legacy:
            return legacy.rstrip("/")

    return str(preset.get("base_url") or "").rstrip("/")


def provider_api_key(preset: dict) -> str:
    """解析 provider 的 env 种子 key。优先级：
    管理端 DB 覆盖 → 目录声明的 ``api_key_env`` → ``{PROVIDER}_API_KEY`` env
    → Settings 的 ``{provider}_api_key`` 字段 → openai 的 ``LLM_API_KEY`` 历史回退。
    """
    pid = str(preset.get("id") or "")
    if not pid:
        return ""
    settings = _settings()

    override = provider_overrides().get(f"{pid}_api_key", "").strip()
    if override:
        return override

    for env_name in (str(preset.get("api_key_env") or ""), _provider_env_name(pid, "_API_KEY")):
        if not env_name:
            continue
        value = os.getenv(env_name, "").strip()
        if value:
            return value

    field_value = str(getattr(settings, f"{pid}_api_key", "") or "").strip()
    if field_value:
        return field_value

    if pid == "openai":
        legacy = str(getattr(settings, "llm_api_key", "") or "").strip()
        if legacy:
            return legacy

    return ""


def provider_requires_key(preset: dict) -> bool:
    """是否需要 key 才能注册（False = 本地/自托管端点可无 key）。"""
    return bool(preset.get("requires_key", True))


def provider_kind(preset: dict) -> str:
    """接入协议：``openai`` | ``anthropic``。"""
    kind = str(preset.get("kind") or "openai").strip().lower()
    return kind if kind in ("openai", "anthropic") else "openai"
