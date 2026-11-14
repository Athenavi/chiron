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


# ── "未执行"不算 repeat（observe_skipped）─────────────────────────────
#
# 实测场景：write_file 的 arguments 被 max_tokens 截断 → 参数非法 JSON → 拒绝执行
# → 模型补全后重发。第 3 次会被 repeat 判死循环，连执行机会都没有：护栏反过来
# 惩罚了自纠。未动手的调用不该占用 repeat 计数。


def test_skipped_calls_do_not_accumulate_repeat():
    g = LoopGuard()
    args = '{"path": "a", "content": "x"}'
    for _ in range(5):
        g.observe_call("write_file", args)
        g.observe_skipped("write_file", args)
    # 每次都被撤回，永远攒不到 3 条相同记录
    assert g.observe_call("write_file", args).hit is False


def test_repeat_still_fires_for_actually_executed_calls():
    # 撤回只对"未执行"生效：真执行过的重复调用照旧被拦（否则护栏形同虚设）
    g = LoopGuard()
    args = {"path": "a"}
    g.observe_call("read_file", args)
    g.observe_call("read_file", args)
    verdict = g.observe_call("read_file", args)
    assert verdict.kind == REPEAT


def test_observe_skipped_only_withdraws_matching_tail():
    # 并发批里多个调用交错登记：只弹最后一条且指纹相同的，不碰别人的记录
    g = LoopGuard()
    g.observe_call("read_file", {"path": "a"})
    g.observe_call("grep_files", {"query": "x"})
    assert g.observe_skipped("read_file", {"path": "a"}) is False
    # 不匹配时队列不变，`grep_files` 那条仍在
    assert g.observe_skipped("grep_files", {"query": "x"}) is True


def test_tool_skipped_reported():
    g = LoopGuard()
    assert g.observe_skipped("grep_files", {"query": "x"}) is False


def test_hint_states_the_count():
    g = LoopGuard()
    verdict = None
    for _ in range(3):
        verdict = g.observe_result({"same": True})
    assert verdict is not None
    hint = loop_hint(verdict)
    assert hint and "3" in hint
