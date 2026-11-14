"""Super-agent 看门狗（app/agent/watchdog.py + app/queue/watchdog_job.py）。

回归保护三条验收：

1. 对"兜圈子"回放样本给出**与 loop_guard 一致或更早**的信号；
2. 关掉时**零影响** —— 连采集都不发生；
3. 每次干预都有审计。

另有三条踩过的坑值得留在这里：

- **``Sample.usable`` 必须显式检查**：``turn_status`` 不是 ``running`` 的回合（刚结束、
  已取消）内容必然不再变化，若照常参与判断，每一个正常结束的回合都会被判成"停滞"。
- **``message_count`` 必须一起比较**：模型只写文件、不回复时 `content_digest` 不变，
  光看内容会把"正在干活"误判为"卡住"。
- **样本不足即 ``CONTINUE``**：租约漂到另一个副本时进程内历史归零，这时唯一站得住的
  结论是"不知道，别动"。
"""

import pytest

from app.agent.watchdog import (
    DEFAULT_NUDGE_AFTER,
    DEFAULT_STOP_AFTER,
    Sample,
    Verdict,
    assess,
    digest,
    nudge_text,
)
from app.queue.watchdog_job import reset_history, tick
from app.queue.watchdog_probe import TurnSample


def _sample(*, content="c1", tools="t1", count=10, status="running", turn="turn-1"):
    return Sample(
        turn_id=turn,
        turn_status=status,
        content_digest=content,
        tool_digest=tools,
        message_count=count,
    )


def _turn(sample):
    return TurnSample(
        sample=sample,
        tenant_id=1,
        user_id="u-1",
        session_id="s-1",
    )


class _FakeRedis:
    """只实现 tick 用到的三个命令；``available=False`` 模拟 Redis 故障。"""

    def __init__(self, *, available=True):
        self.available = available
        self.store = {}

    async def set(self, key, value, nx=False, ex=None):
        if not self.available:
            raise RuntimeError("redis down")
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, *keys):
        if not self.available:
            raise RuntimeError("redis down")
        for key in keys:
            self.store.pop(key, None)
        return 1


class _RecordingPool:
    """记录 audit 写入，不连数据库。"""

    def __init__(self):
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "1"


# ── 判断器：三档 ──


def test_single_sample_is_never_enough():
    """一拍无从谈起"连续"——必须 CONTINUE。"""
    verdict = assess([_sample()])
    assert verdict.verdict is Verdict.CONTINUE
    assert verdict.stalled == 0


def test_repeated_sample_nudges_at_threshold():
    samples = [_sample() for _ in range(DEFAULT_NUDGE_AFTER)]
    verdict = assess(samples)
    assert verdict.verdict is Verdict.NUDGE
    assert verdict.stalled == DEFAULT_NUDGE_AFTER


def test_repeated_sample_escalates_to_suggest_stop():
    samples = [_sample() for _ in range(DEFAULT_STOP_AFTER)]
    verdict = assess(samples)
    assert verdict.verdict is Verdict.SUGGEST_STOP
    assert verdict.stalled == DEFAULT_STOP_AFTER


def test_progress_resets_the_counter():
    """中间有一拍产生了新内容，序列尾部只剩 1 拍相同 → 不报警。"""
    samples = [_sample(content="a"), _sample(content="a"), _sample(content="b")]
    verdict = assess(samples)
    assert verdict.verdict is Verdict.CONTINUE
    assert verdict.stalled == 1


def test_tail_of_two_identical_samples_is_at_the_nudge_threshold():
    """`assess` 数的是"含本拍在内的连续拍数"：末尾两拍相同即 `stalled == 2`。"""
    samples = [_sample(content="a"), _sample(content="b"), _sample(content="b")]
    verdict = assess(samples)
    assert verdict.stalled == 2
    assert verdict.verdict is Verdict.NUDGE


def test_message_count_growth_counts_as_progress():
    """只写文件不回复：内容哈希不变，但条数在涨 —— 是在干活，不是卡住。"""
    samples = [_sample(count=10), _sample(count=11), _sample(count=12)]
    assert assess(samples).verdict is Verdict.CONTINUE


def test_tool_change_counts_as_progress():
    samples = [_sample(tools="a"), _sample(tools="b"), _sample(tools="c")]
    assert assess(samples).verdict is Verdict.CONTINUE


def test_finished_turn_is_ignored():
    """已结束的回合内容当然不再变化 —— 不能因此被判成停滞。"""
    samples = [_sample(), _sample(), _sample(), _sample()]
    for s in samples:
        object.__setattr__(s, "turn_status", "completed")
    assert assess(samples).verdict is Verdict.CONTINUE


def test_empty_digest_is_ignored():
    """没读到消息 ≠ 没有进展。把它算作"无变化"会凭空制造误报。"""
    blank = Sample(turn_id="turn-1", turn_status="running", content_digest="")
    assert assess([blank, blank, blank]).verdict is Verdict.CONTINUE


def test_disabled_thresholds_never_act():
    samples = [_sample() for _ in range(10)]
    assert assess(samples, nudge_after=0, stop_after=0).verdict is Verdict.CONTINUE


def test_nudge_text_only_for_nudge():
    assert nudge_text(assess([_sample(), _sample()])).startswith("【进度检查】")
    assert nudge_text(assess([_sample()])) == ""
    assert nudge_text(assess([_sample() for _ in range(DEFAULT_STOP_AFTER)])) == ""


def test_digest_is_order_sensitive():
    """顺序变了就是内容变了 —— 过强的归一化会抹掉真实的进展信号。"""
    assert digest(["a", "b"]) != digest(["b", "a"])
    assert digest(["a", "b"]) == digest(["a", "b"])


# ── 判断器与 loop_guard 的对照 ──


def test_earlier_signal_than_loop_guard_when_configured():
    """验收①：同一段"兜圈子"序列，watchdog 可配置得比 loop_guard 更早报警。

    ``loop_guard`` 的 repeat 阈值是 **3 次同参调用**；这里把 ``nudge_after`` 调到 1，
    第 2 拍就已经给出信号 —— 而 loop_guard 要到第 3 次调用才命中。
    """
    from app.agent.loop_guard import REPEAT, LoopGuard

    # watchdog 一侧：两拍即可 NUDGE
    verdict = assess([_sample(), _sample()], nudge_after=1, stop_after=0)
    assert verdict.verdict is Verdict.NUDGE

    # loop_guard 一侧：同样两次同参调用还没命中
    guard = LoopGuard()
    guard.observe_call("read_file", {"path": "a.txt"})
    assert guard.observe_call("read_file", {"path": "a.txt"}).hit is False, (
        "loop_guard 在第 2 次同参调用时仍未命中，watchdog 已可报警（更早）"
    )
    assert guard.observe_call("read_file", {"path": "a.txt"}).kind == REPEAT


# ── 调度面：零影响 / 审计 / 动作 ──


async def test_tick_disabled_does_nothing():
    """验收②：关掉时连采集都不发生。"""
    called = []

    async def _collect():
        called.append(1)
        return []

    result = await tick(redis=_FakeRedis(), enabled=False, collect_fn=_collect)
    assert result.skipped == "disabled"
    assert result.observed == 0
    assert called == [], "enabled=False 时不得触碰数据库"


async def test_tick_skips_when_lease_is_taken():
    redis = _FakeRedis()
    redis.store["engine:watchdog:lease"] = "other-replica"
    result = await tick(redis=redis, enabled=True, collect_fn=_no_turns)
    assert result.skipped == "lease_busy"


async def test_tick_redis_down_does_not_break_the_turn():
    result = await tick(redis=_FakeRedis(available=False), enabled=True, collect_fn=_no_turns)
    assert result.skipped == "lease_busy"


async def test_observe_only_writes_audit_but_does_not_nudge(steer_sink):
    """验收③：开着观察、没开动作 —— 结论只进审计，不注入提示。"""
    pool = _RecordingPool()

    turns = [_turn(_sample()) for _ in range(DEFAULT_NUDGE_AFTER)]
    # 每拍是一次 tick：历史在进程内累积，所以逐拍喂
    for i in range(DEFAULT_NUDGE_AFTER):
        result = await tick(
            redis=_FakeRedis(),
            enabled=True,
            act=False,
            collect_fn=_fixed([turns[i]]),
            pool=pool,
        )
    assert result.verdicts, "应当已经判断出非 CONTINUE"
    assert result.verdicts[0][1].verdict is Verdict.NUDGE
    assert steer_sink == [], "act=False 时不得往会话里插提示"
    assert pool.executed, "观察档也必须留下审计"
    action = pool.executed[-1][1][3]
    assert action == "superagent.watchdog.nudge"


async def test_act_pushes_nudge_through_steer(steer_sink):
    pool = _RecordingPool()

    for _ in range(DEFAULT_NUDGE_AFTER):
        result = await tick(
            redis=_FakeRedis(),
            enabled=True,
            act=True,
            collect_fn=_fixed([_turn(_sample())]),
            pool=pool,
        )
    assert steer_sink, "act=True 且判出 NUDGE 时必须投递提示"
    session_id, text = steer_sink[-1]
    assert session_id == "s-1"
    assert text.startswith("【进度检查】")
    assert result.acted == 1
    assert pool.executed[-1][1][3] == "superagent.watchdog.nudge"
    assert pool.executed[-1][1][-1] is not None  # created_at 有值


async def test_suggest_stop_never_terminates_even_with_act(steer_sink):
    """终止是不可逆动作 —— 这一档永远只留证据。"""
    pool = _RecordingPool()

    for _ in range(DEFAULT_STOP_AFTER):
        result = await tick(
            redis=_FakeRedis(),
            enabled=True,
            act=True,
            collect_fn=_fixed([_turn(_sample())]),
            pool=pool,
        )
    assert result.verdicts[-1][1].verdict is Verdict.SUGGEST_STOP
    assert [args[3] for _, args in pool.executed][-1] == "superagent.watchdog.suggest_stop"
    assert result.acted == 0, "建议终止不得计入 acted"


async def test_audit_failure_is_fail_soft():
    reset_history()

    class _BrokenPool:
        async def execute(self, query, *args):
            raise RuntimeError("audit table missing")

    for _ in range(DEFAULT_NUDGE_AFTER):
        result = await tick(
            redis=_FakeRedis(),
            enabled=True,
            act=False,
            collect_fn=_fixed([_turn(_sample())]),
            pool=_BrokenPool(),
        )
    # 审计写不进去不该让判断消失
    assert result.verdicts
    assert result.verdicts[0][1].verdict is Verdict.NUDGE


async def test_history_is_pruned_when_turn_leaves_running():
    reset_history()
    pool = _RecordingPool()

    await tick(
        redis=_FakeRedis(),
        enabled=True,
        collect_fn=_fixed([_turn(_sample())]),
        pool=pool,
    )
    await tick(redis=_FakeRedis(), enabled=True, collect_fn=_no_turns, pool=pool)

    from app.queue import watchdog_job

    assert watchdog_job._HISTORY == {}


def _fixed(turns):
    async def _collect():
        return turns

    return _collect


async def _no_turns():
    return []


@pytest.fixture
def steer_sink(monkeypatch):
    """替换 ``push_steer``，避免测试碰 Redis；记录调用便于断言。

    用 ``monkeypatch`` 而非直接赋值：直接改模块属性会把假件留给同一进程里的
    **其它测试文件**（pytest 不重置已导入模块的全局），那种污染只在全量跑时才现形。
    """
    sink = []

    async def _fake(session_id, text, *, source="user", redis=None):
        sink.append((session_id, text))
        return True, ""

    import app.agent.steer as steer_mod

    monkeypatch.setattr(steer_mod, "push_steer", _fake)
    return sink


@pytest.fixture(autouse=True)
def _reset():
    reset_history()
    yield
    reset_history()
