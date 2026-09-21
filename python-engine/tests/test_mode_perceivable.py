"""四种模式必须"可感知"。

回归背景（2026-09 实测）：PTC 与 CREATIVE 的 ``include_tools`` 与 normal 完全相同，
且 PTC 没有 persona —— 于是用户在界面上"切换模式"后感觉**没有任何变化**，
这就是"模式切换无效"的真实根因（不是链路没传值，而是模式本身没有可感知差异）。

本测试锁死：任一模式在 (工具集, persona) 上必须与其他模式可区分，
并且每个模式的**定位**要体现在配置里（极简要真极简、PTC 要有程序化调用指引、
创造要能改自身模式定义）。
"""

from app.agent.modes import get_mode_config

MODES = ("normal", "minimal", "ptc", "creative")


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


def test_ptc_directs_program_aided_tool_calling():
    """PTC 的差异在"用工具的方式"：必须体现为 persona 指引 + run_code 可用。"""
    cfg = get_mode_config("ptc")
    assert "run_code" in (cfg.include_tools | cfg.extra_tools)
    assert cfg.persona, "PTC 缺少 persona → 与常规模式不可区分"
    assert "run_code" in cfg.persona


def test_creative_can_edit_own_modes():
    """创造 = 可以读写平台自身的模式定义（自我改造）。"""
    cfg = get_mode_config("creative")
    assert {"mode_list", "mode_edit"} <= (cfg.include_tools | cfg.extra_tools)
    assert cfg.persona


def test_unknown_mode_falls_back_to_normal():
    assert _fingerprint("does-not-exist") == _fingerprint("normal")
    assert _fingerprint("") == _fingerprint("normal")
