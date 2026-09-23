"""循环护栏（app/agent/loop_guard.py）。

回归保护：这类"卡在循环里"的形态，现有各轴预算都拦不住 —— 轮次没超、时间没超、
token 还在涨，agent 只是在原地打转。

另有一条实测踩过的坑：dataclass 默认 **truthy**，于是 `detect_a() or detect_b()` 被
"未命中"的结果短路，`oscillation` 永远不会被检查到 —— 见 ``LoopVerdict.__bool__``。
"""
from app.agent.loop_guard import NO_PROGRESS, OSCILLATION, REPEAT, LoopGuard, loop_hint


def test_repeat_detected_after_threshold():
    g = LoopGuard()
    assert g.observe_call("read_file", {"path": "a"}).hit is False
    assert g.observe_call("read_file", {"path": "a"}).hit is False
    verdict = g.observe_call("read_file", {"path": "a"})
    assert verdict.kind == REPEAT
    assert verdict.count == 3


def test_repeat_ignores_cosmetic_arg_differences():
    # "./a.txt" 与 "a.txt" 是同一次调用；归一化后必须同哈希
    g = LoopGuard()
    g.observe_call("read_file", {"path": "a.txt"})
    g.observe_call("read_file", {"path": "./a.txt"})
    verdict = g.observe_call("read_file", {"path": "a.txt"})
    assert verdict.kind == REPEAT


def test_repeat_accepts_raw_json_arguments():
    # 集成点直接把模型给的原始 arguments 字符串传进来，不必先解析
    g = LoopGuard()
    g.observe_call("read_file", '{"path": "a"}')
    g.observe_call("read_file", '{"path": "./a"}')
    assert g.observe_call("read_file", '{"path":"a"}').kind == REPEAT


def test_different_calls_do_not_trigger_repeat():
    g = LoopGuard()
    for path in ("a", "b", "c"):
        assert g.observe_call("read_file", {"path": path}).hit is False


def test_oscillation_detected_between_two_calls():
    g = LoopGuard()
    last = None
    for name in ("read_file", "grep_files") * 2:
        last = g.observe_call(name, {})
    assert last is not None
    assert last.kind == OSCILLATION
    assert last.count == 4


def test_no_progress_on_identical_results():
    g = LoopGuard()
    same = {"output": "nothing changed"}
    g.observe_result(same)
    g.observe_result(same)
    verdict = g.observe_result(same)
    assert verdict.kind == NO_PROGRESS
    assert verdict.count == 3


def test_progress_breaks_the_chain():
    g = LoopGuard()
    g.observe_result({"output": "a"})
    g.observe_result({"output": "a"})
    assert g.observe_result({"output": "b"}).hit is False


def test_thresholds_can_be_disabled():
    g = LoopGuard(repeat_threshold=0, no_progress_threshold=0, oscillation_threshold=0)
    for _ in range(5):
        assert g.observe_call("read_file", {"path": "a"}).hit is False
        assert g.observe_result({"x": 1}).hit is False


def test_verdict_is_falsy_when_no_hit():
    # dataclass 默认 truthy 会让 `a or b` 短路（实测 bug）—— 这里守住 __bool__
    g = LoopGuard()
    verdict = g.observe_call("read_file", {"path": "a"})
    assert not verdict
    assert verdict.hit is False


def test_hint_states_the_count():
    g = LoopGuard()
    verdict = None
    for _ in range(3):
        verdict = g.observe_result({"same": True})
    assert verdict is not None
    hint = loop_hint(verdict)
    assert hint and "3" in hint
