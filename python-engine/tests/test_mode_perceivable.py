"""模式必须"可感知"。

回归背景（2026-09 实测）：某些模式的 ``include_tools`` 与 normal 完全相同且没有 persona
—— 于是用户在界面上"切换模式"后感觉**没有任何变化**，这就是"模式切换无效"的真实根因
（不是链路没传值，而是模式本身没有可感知差异）。

本测试锁死：现存模式在 (工具集, persona) 上必须可区分，并且每个模式的**定位**要体现在
配置里（极简要真极简）。PTC / creative 已移除，旧值必须回退到 normal。
"""

from app.agent.modes import get_mode_config

MODES = ("normal", "minimal")


def _fingerprint(mode: str) -> dict:
    cfg = get_mode_config(mode)
    return {
        "tools": sorted(cfg.include_tools | cfg.extra_tools),
        "persona": cfg.persona or "",
        "include_context": cfg.include_context,
        "enable_compaction": cfg.enable_compaction,
    }


def test_every_mode_is_distinguishable():
    """任意两个模式不得在 (工具集, persona) 上完全相同 —— 否则切了等于没切。"""
    seen: dict[tuple, str] = {}
    for mode in MODES:
        fp = _fingerprint(mode)
        key = (tuple(fp["tools"]), fp["persona"])
        assert key not in seen, (
            f"{mode} 与 {seen[key]} 的 (工具集, persona) 完全相同 → 用户切换时不可感知"
        )
        seen[key] = mode


def test_minimal_is_actually_minimal():
    """极简 = 只读/改/跑，不注入上下文，不做压缩。"""
    cfg = get_mode_config("minimal")
    assert (cfg.include_tools | cfg.extra_tools) == {
        "read_file",
        "edit_file",
        "shell_exec",
    }
    assert cfg.include_context is False
    assert cfg.enable_compaction is False


def test_ptc_and_creative_fall_back_to_normal():
    """PTC / creative 已移除：旧客户端或旧配置传入这些值时必须安静回退。"""
    for legacy in ("ptc", "creative"):
        assert _fingerprint(legacy) == _fingerprint("normal"), (
            f"{legacy} 未回退到 normal → 旧客户端会拿到一个不存在的模式配置"
        )


def test_unknown_mode_falls_back_to_normal():
    assert _fingerprint("does-not-exist") == _fingerprint("normal")
    assert _fingerprint("") == _fingerprint("normal")
