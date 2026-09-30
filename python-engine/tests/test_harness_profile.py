"""B1（方案 04 §3）：按**模型家族**的 harness 塑形。

最重要的性质是**未注册 ⇒ 逐字不变**：引入这个机制不该悄悄改变任何现有模型的输出。所以本文件的
一半断言是在测"没注册时什么都不发生"。

另一半在测匹配规则 —— 尤其是"更具体的前缀要赢过更一般的"，因为那个机制存在的理由就是让具体
覆盖一般。
"""

from __future__ import annotations

from app.agent.harness_profile import (
    HarnessProfile,
    apply_harness_suffix,
    match_harness_profile,
    register_harness_profile,
    registered_harness_profiles,
    reset_harness_profiles,
)
from app.agent.runtime import AgentTask, _apply_system_prefix

PROMPT = "You are Chiron."


def setup_function() -> None:
    reset_harness_profiles()


def teardown_function() -> None:
    reset_harness_profiles()


# ── 未注册 ⇒ 逐字不变（最重要）──────────────────────────────────────────


def test_empty_registry_is_identity():
    assert registered_harness_profiles() == ()
    assert apply_harness_suffix(PROMPT, model="deepseek-v4-flash") == PROMPT
    assert apply_harness_suffix(PROMPT, model="") == PROMPT
    assert apply_harness_suffix("", model="anything") == ""


def test_unregistered_model_is_untouched_even_with_registry_populated():
    register_harness_profile(
        HarnessProfile(name="ds", model_prefixes=("deepseek-",), system_prompt_suffix="DS")
    )

    untouched = apply_harness_suffix(PROMPT, model="claude-sonnet-4-20250514")

    assert untouched == PROMPT


def test_prompt_shape_is_unchanged_without_registration():
    """端到端：装配 system 段时，没注册就该与"没有这个机制"完全一致。"""
    task = AgentTask(
        id="t",
        tenant_id="t",
        user_id="u",
        session_id="s",
        content="hi",
        system_prompt=PROMPT,
        llm_config={"model": "deepseek-v4-flash"},
    )

    messages = _apply_system_prefix([{"role": "user", "content": "hi"}], task)

    assert messages[0]["content"] == PROMPT


# ── 匹配规则 ────────────────────────────────────────────────────────────


def test_suffix_is_appended_last():
    register_harness_profile(
        HarnessProfile(name="ds", model_prefixes=("deepseek-",), system_prompt_suffix="DS")
    )

    assert apply_harness_suffix(PROMPT, model="deepseek-v4-flash") == f"{PROMPT}\n\nDS"


def test_suffix_only_prompt_still_works_without_base():
    register_harness_profile(
        HarnessProfile(name="ds", model_prefixes=("deepseek-",), system_prompt_suffix="DS")
    )

    assert apply_harness_suffix("", model="deepseek-chat") == "DS"


def test_more_specific_prefix_wins():
    """`gpt-` 不该吃掉 `gpt-5-codex` —— 具体覆盖一般正是这个机制的理由。"""
    register_harness_profile(
        HarnessProfile(name="openai", model_prefixes=("gpt-",), system_prompt_suffix="GENERIC")
    )
    register_harness_profile(
        HarnessProfile(name="codex", model_prefixes=("gpt-5-codex",), system_prompt_suffix="CODEX")
    )

    assert match_harness_profile("gpt-5-codex").name == "codex"  # type: ignore[union-attr]
    assert apply_harness_suffix(PROMPT, model="gpt-5-codex") == f"{PROMPT}\n\nCODEX"
    assert apply_harness_suffix(PROMPT, model="gpt-4o") == f"{PROMPT}\n\nGENERIC"


def test_matching_is_case_insensitive_and_trims():
    register_harness_profile(
        HarnessProfile(name="ds", model_prefixes=("DeepSeek-",), system_prompt_suffix="DS")
    )

    assert match_harness_profile("  deepseek-v4  ") is not None


def test_reregistering_same_name_replaces():
    register_harness_profile(
        HarnessProfile(name="ds", model_prefixes=("deepseek-",), system_prompt_suffix="OLD")
    )
    register_harness_profile(
        HarnessProfile(name="ds", model_prefixes=("deepseek-",), system_prompt_suffix="NEW")
    )

    assert len(registered_harness_profiles()) == 1
    assert apply_harness_suffix(PROMPT, model="deepseek-x").endswith("NEW")


def test_blank_prefix_never_matches_everything():
    """空前缀若不拦，会变成"匹配所有模型"——那正是最危险的默认。"""
    register_harness_profile(
        HarnessProfile(name="bad", model_prefixes=("", "  "), system_prompt_suffix="BAD")
    )

    assert match_harness_profile("any-model") is None


def test_profile_without_suffix_does_not_change_prompt():
    """只声明 params（未接线）的 profile 不该动 prompt。"""
    register_harness_profile(
        HarnessProfile(name="ds", model_prefixes=("deepseek-",), system_prompt_suffix="")
    )

    assert apply_harness_suffix(PROMPT, model="deepseek-v4") == PROMPT


def test_end_to_end_suffix_lands_in_system_message():
    register_harness_profile(
        HarnessProfile(name="ds", model_prefixes=("deepseek-",), system_prompt_suffix="DS-RULES")
    )
    task = AgentTask(
        id="t",
        tenant_id="t",
        user_id="u",
        session_id="s",
        content="hi",
        system_prompt=PROMPT,
        llm_config={"model": "deepseek-v4-flash"},
        memory_context="memory block",
    )

    messages = _apply_system_prefix([{"role": "user", "content": "hi"}], task)

    assert messages[0]["content"] == f"{PROMPT}\n\nDS-RULES"
    # 记忆仍是**独立**的 system 消息（C2），没被 suffix 吃进去
    assert messages[1]["content"] == "memory block"
