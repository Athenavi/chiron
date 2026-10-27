"""loopback 推理代理的客户端（契约 §4：core 自己不持有 provider key）。

调用形状与 W4 的 ``frontend-vue/electron/inference-proxy.cjs`` 对齐：

- 请求：``POST {proxy}/infer``，body ``{tenant_id, user_id, model, messages, max_tokens, temperature}``
  （代理只白名单透传 ``model/messages/temperature/max_tokens``，``tenant_id`` / ``user_id``
  被忽略——这里仍照发，是为了让日志/诊断能对上租户，且将来代理做配额时不用改请求方）。
- 响应：``{"content": "...", "usage": {"prompt_tokens", "completion_tokens", "total_tokens"},
  "provider": "...", "model": "..."}``。

两条硬约束：

1. **未配置代理直接 503**，不要假装本地能推理（本地 Ollama 是否在跑由代理决定）。
2. 错误只回状态码，**不回显上游正文**——上游可能把 prompt 原文塞进错误信息（契约 §8：
   不把用户内容写进日志/错误）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from app.runtime.store import DEFAULT_TENANT_ID, DEFAULT_USER_ID

#: 代理端口与路径（与 W4 的 ``inference-proxy.cjs`` 一致）
INFER_PATH = "/infer"

#: 契约要求的超时：本地大模型首 token 慢，120s 是上限不是期望值
DEFAULT_TIMEOUT_SECONDS = 120.0

#: 代理的一次性 token 从环境变量取（``inference_proxy`` 只存地址，没有密钥字段）
TOKEN_ENV = "CHIRON_LOCAL_INFERENCE_TOKEN"

#: 可用模型清单（JSON 数组，元素是字符串或 ``{"id": ...}`` 对象）
MODELS_ENV = "CHIRON_LOCAL_MODELS"

#: ``POST /v1/models/verify`` 的探测预算：只验"通不通"，不要烧 token
VERIFY_MAX_TOKENS = 16
VERIFY_PROMPT = "ping"


class InferenceError(RuntimeError):
    """推理链路失败。``status_code`` 由 API 层直接用作 HTTP 状态码。"""

    status_code = 502


class InferenceNotConfiguredError(InferenceError):
    """没有配置推理代理（``CHIRON_LOCAL_INFERENCE_PROXY`` 为空）→ 503。"""

    status_code = 503


class InferenceTimeoutError(InferenceError):
    """代理超时 → 504（请求没写错，是上游太慢）。"""

    status_code = 504


class InferenceUpstreamError(InferenceError):
    """代理返回错误 / 响应不可解析 → 502。"""

    status_code = 502


class ModelConfigError(ValueError):
    """``CHIRON_LOCAL_MODELS`` 不是合法 JSON 或形状不对。"""


# --------------------------------------------------------------------- 模型清单
def load_configured_models(raw: str | None = None) -> list[dict[str, Any]]:
    """把 ``CHIRON_LOCAL_MODELS`` 解析成 ``[{"id": ..., ...}]``。

    接受的形状（都归一化成同一种输出）：

    - ``[]`` / 空串 → ``[]``（默认：不编造任何模型）
    - ``["llama3.2", "qwen2.5:7b"]``
    - ``[{"id": "qwen2.5:7b", "label": "Qwen2.5 7B", "context_window": 32768}]``
    - ``{"models": [...]}``（包一层对象也行）

    配置写错时**抛错**而不是静默返回空列表：用户明明填了东西却看不到模型，
    比看到一条"JSON 第 3 行有错"更难排查。
    """
    text = (os.environ.get(MODELS_ENV, "") if raw is None else raw) or ""
    if not text.strip():
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelConfigError(f"{MODELS_ENV} 不是合法 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）") from exc

    if isinstance(parsed, dict):
        parsed = parsed.get("models", [])
    if not isinstance(parsed, list):
        raise ModelConfigError(f'{MODELS_ENV} 必须是 JSON 数组或 {{"models": [...]}}')

    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(parsed):
        if isinstance(item, str):
            entry: dict[str, Any] = {"id": item.strip()}
        elif isinstance(item, dict):
            entry = {}
            for key in ("id", "label", "provider", "context_window"):
                if item.get(key) is not None:
                    entry[key] = item[key]
            if not isinstance(entry.get("id"), str):
                entry["id"] = str(item.get("id") or item.get("name") or "").strip()
        else:
            raise ModelConfigError(f"{MODELS_ENV}[{index}] 必须是字符串或对象，收到 {type(item).__name__}")

        model_id = entry.get("id") or ""
        if not model_id:
            raise ModelConfigError(f"{MODELS_ENV}[{index}] 缺少 id")
        if model_id in seen:
            continue
        seen.add(model_id)
        models.append(entry)
    return models


def first_model_id() -> str:
    """配置里第一个模型的 id（空配置返回空串）。"""
    models = load_configured_models()
    return str(models[0]["id"]) if models else ""


# --------------------------------------------------------------------- 客户端
class InferenceClient:
    """调 ``{base_url}/infer`` 的最小客户端（每次请求一条 httpx 连接池）。"""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @property
    def infer_url(self) -> str:
        return f"{self.base_url}{INFER_PATH}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def infer(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        tenant_id: int = DEFAULT_TENANT_ID,
        user_id: str = DEFAULT_USER_ID,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """调一次推理，返回归一化结果。

        返回 ``{"text", "model", "provider", "usage", "usage_estimated"}``；代理没给 usage
        时用字符数估算并标记 ``usage_estimated=True``（不冒充上游真实计量）。
        """
        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "model": model,
            "messages": messages,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.infer_url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"推理代理超过 {self.timeout:.0f}s 未返回") from exc
        except httpx.HTTPError as exc:
            # 只带异常类型：代理/上游的报错正文可能含 prompt 原文
            raise InferenceUpstreamError(f"连不上推理代理（{type(exc).__name__}）") from exc

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        if response.status_code >= 400:
            raise InferenceUpstreamError(f"推理代理返回 HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise InferenceUpstreamError("推理代理返回的不是 JSON") from exc
        if not isinstance(body, dict):
            raise InferenceUpstreamError("推理代理返回的 JSON 不是对象")

        text = body.get("content")
        if not isinstance(text, str):
            text = body.get("text") if isinstance(body.get("text"), str) else ""

        usage_payload = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        usage, estimated = _normalize_usage(usage_payload, messages, text)

        resolved_model = body.get("model") if isinstance(body.get("model"), str) else ""
        return {
            "text": text,
            "model": resolved_model or model,
            "provider": str(body.get("provider") or ""),
            "usage": usage,
            "usage_estimated": estimated,
            "latency_ms": elapsed_ms,
        }


def _normalize_usage(
    usage_payload: dict[str, Any],
    messages: list[dict[str, str]],
    text: str,
) -> tuple[dict[str, int], bool]:
    """读代理给的 usage；缺失时按字符数估算并标记 ``estimated``。"""

    def _as_int(key: str) -> int:
        try:
            return max(0, int(usage_payload.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    prompt_tokens = _as_int("prompt_tokens")
    completion_tokens = _as_int("completion_tokens")
    if prompt_tokens or completion_tokens:
        total = _as_int("total_tokens") or prompt_tokens + completion_tokens
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total,
        }, False

    # 粗略估算：4 字符 ≈ 1 token（中西文混排的常见量级），仅用于展示，别当计量依据
    prompt_chars = sum(len(message.get("content") or "") for message in messages)
    estimated_prompt = max(1, round(prompt_chars / 4))
    estimated_completion = max(1, round(len(text) / 4)) if text else 0
    return {
        "prompt_tokens": estimated_prompt,
        "completion_tokens": estimated_completion,
        "total_tokens": estimated_prompt + estimated_completion,
    }, True


# --------------------------------------------------------------------- 取用
def inference_for(state: Any) -> Any:
    """从 ``app.state`` 取推理客户端。

    优先级：``state.local_inference``（测试/父级接线的注入点，鸭子类型：只要有
    ``async infer(...)``）→ 按 ``settings.inference_proxy`` 构造并缓存 → 都没有则抛
    :class:`InferenceNotConfiguredError`（API 转 503）。
    """
    injected = getattr(state, "local_inference", None)
    if injected is not None:
        return injected

    base_url = (getattr(getattr(state, "settings", None), "inference_proxy", "") or "").strip()
    if not base_url:
        raise InferenceNotConfiguredError(
            "未配置推理代理（CHIRON_LOCAL_INFERENCE_PROXY / --inference-proxy）；本地模式无法凭空推理"
        )

    token = os.environ.get(TOKEN_ENV, "").strip()
    cached = getattr(state, "_local_inference", None)
    if isinstance(cached, InferenceClient) and cached.base_url == base_url.rstrip("/") and cached.token == token:
        return cached

    client = InferenceClient(base_url, token=token)
    state._local_inference = client
    return client
