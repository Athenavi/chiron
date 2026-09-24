"""模型自动降级 — Provider 失败时自动切换备用模型。

降级策略：
1. 同 provider 不同模型（如 gpt-4 → gpt-3.5-turbo）
2. 不同 provider 同能力等级（如 gpt-4 → claude-3-opus）
3. 不同 provider 低等级（如 gpt-4 → claude-3-haiku）

用法：
  degrade = ModelDegrader()
  fallback = degrade.select("gpt-4", failed_provider="openai")
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# 模型等级映射：model_name → (等级, provider, 系列)
MODEL_TIERS: dict[str, tuple[int, str, str]] = {
    # 旗舰
    "gpt-4":           (5, "openai",    "gpt-4"),
    "gpt-4-turbo":     (5, "openai",    "gpt-4"),
    "gpt-4o":          (5, "openai",    "gpt-4o"),
    "claude-3-opus":   (5, "anthropic", "claude-3"),
    "claude-3.5-sonnet": (5, "anthropic", "claude-3.5"),
    "claude-3.5-haiku": (4, "anthropic", "claude-3.5"),
    # 中档
    "gpt-4o-mini":     (4, "openai",    "gpt-4o"),
    "claude-3-sonnet": (4, "anthropic", "claude-3"),
    "deepseek-chat":   (4, "deepseek",  "deepseek"),
    # 经济
    "gpt-3.5-turbo":   (3, "openai",    "gpt-3.5"),
    "claude-3-haiku":  (3, "anthropic", "claude-3"),
    "deepseek-reasoner": (3, "deepseek", "deepseek"),
    "gemini-pro":      (4, "google",    "gemini"),
    "gemini-1.5-flash":(3, "google",    "gemini-1.5"),
}

# 降级路径：同一系列内降级
SERIES_FALLBACK: dict[str, str] = {
    "gpt-4": "gpt-4o-mini",
    "gpt-4o": "gpt-4o-mini",
    "claude-3-opus": "claude-3-sonnet",
    "claude-3.5-sonnet": "claude-3.5-haiku",
    "claude-3-sonnet": "claude-3-haiku",
}

# 跨 provider 同级替代
PEER_ALTERNATIVES: dict[str, list[str]] = {
    "gpt-4": ["claude-3-opus", "deepseek-chat"],
    "gpt-4o": ["claude-3.5-sonnet", "deepseek-chat"],
    "gpt-4o-mini": ["claude-3.5-haiku", "deepseek-reasoner"],
    "gpt-3.5-turbo": ["claude-3-haiku", "gemini-1.5-flash"],
    "claude-3-opus": ["gpt-4", "deepseek-chat"],
    "claude-3.5-sonnet": ["gpt-4o", "deepseek-chat"],
    "claude-3.5-haiku": ["gpt-4o-mini", "deepseek-reasoner"],
    "claude-3-sonnet": ["gpt-4o-mini"],
    "claude-3-haiku": ["gpt-3.5-turbo", "gemini-1.5-flash"],
    "deepseek-chat": ["gpt-4o-mini", "claude-3.5-haiku"],
    "deepseek-reasoner": ["gpt-3.5-turbo"],
}


class ModelDegrader:
    """模型自动降级选择器。

    按优先级选择降级目标：
    1. 同系列低一级模型（最快切换，API 兼容性最高）
    2. 不同 provider 同级模型（能力接近）
    3. 不同 provider 低一级模型（确保可用性）
    """

    def __init__(self, custom_tiers: dict[str, tuple[int, str, str]] | None = None):
        self._tiers = {**MODEL_TIERS, **(custom_tiers or {})}

    def select(
        self,
        original_model: str,
        failed_provider: str | None = None,
        exclude: set[str] | None = None,
    ) -> str | None:
        """选择降级模型。

        Args:
            original_model: 原始模型名
            failed_provider: 已失败的 provider 名（避免再次选择同一 provider）
            exclude: 额外排除的模型集合

        Returns:
            降级后的模型名，或 None（无可用降级）
        """
        excluded = set(exclude or [])
        original_info = self._tiers.get(original_model)

        # 1. 同系列降级（优先）
        fallback = SERIES_FALLBACK.get(original_model)
        if fallback and fallback not in excluded:
            fb_info = self._tiers.get(fallback)
            if fb_info and (not failed_provider or fb_info[1] != failed_provider):
                logger.info(
                    "Degrade %s -> %s (series fallback)", original_model, fallback
                )
                return fallback

        # 2. 同级跨 provider
        peers = PEER_ALTERNATIVES.get(original_model, [])
        for peer in peers:
            if peer in excluded:
                continue
            peer_info = self._tiers.get(peer)
            if peer_info and (
                not failed_provider or peer_info[1] != failed_provider
            ):
                logger.info(
                    "Degrade %s -> %s (peer alternative)", original_model, peer
                )
                return peer

        # 3. 任意可用的低等级模型
        if original_info:
            original_tier = original_info[0]
            candidates = [
                (name, info)
                for name, info in self._tiers.items()
                if info[0] < original_tier
                and name not in excluded
                and (not failed_provider or info[1] != failed_provider)
            ]
            if candidates:
                # 选等级最高的降级目标
                candidates.sort(key=lambda x: x[1][0], reverse=True)
                chosen = candidates[0][0]
                logger.info(
                    "Degrade %s -> %s (lower tier fallback)", original_model, chosen
                )
                return chosen

        logger.warning("No degrade target for %s", original_model)
        return None

    def degrade_chain(
        self, original_model: str, failed_provider: str | None = None
    ) -> list[str]:
        """返回完整的降级链（从高到低）。"""
        chain: list[str] = []
        excluded: set[str] = set()
        current = original_model

        for _ in range(10):  # 最多 10 步防无限循环
            next_model = self.select(current, failed_provider, exclude=excluded)
            if not next_model or next_model in chain:
                break
            chain.append(next_model)
            excluded.add(next_model)
            excluded.add(current)
            current = next_model

        return chain
