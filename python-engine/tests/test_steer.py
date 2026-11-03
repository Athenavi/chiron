"""steer 队列的来源语义。

「这条指令是谁说的」必须跟着指令一起传到底：用户自己追加的，和 watchdog
自动注入的，可信度不同。以前来源只在调用方参数里转一圈就丢了，历史里
分辨不出来 —— 这组用例把这个口子钉住。
"""

from __future__ import annotations

import pytest

from app.agent import steer


class _Redis:
    """够 steer 用即可：计数、list、rename。"""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.counters: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key: str, start: int, stop: int) -> None:
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if stop == -1 else items[start : stop + 1]

    async def rename(self, src: str, dst: str) -> None:
        if src not in self.lists:
            raise RuntimeError("no such key")
        self.lists[dst] = self.lists.pop(src)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        items = self.lists.get(key, [])
        return items[start:] if stop == -1 else items[start : stop + 1]

    async def delete(self, key: str) -> None:
        self.lists.pop(key, None)


@pytest.fixture(autouse=True)
def _clean_local():
    steer._LOCAL.clear()
    yield
    steer._LOCAL.clear()


def test_encode_decode_roundtrip_keeps_source():
    text = '含引号"与\n换行，还有 emoji 🤖'
    assert steer._decode(steer._encode(text, "watchdog")) == (text, "watchdog")


def test_legacy_plain_text_is_still_delivered():
    """滚动升级期旧副本还在写裸文本：读端不能丢，只能按 `user` 处理。

    宁可标错来源，也不能静默吞掉用户的一条指令。
    """
    assert steer._decode("裸指令") == ("裸指令", "user")


@pytest.mark.asyncio
async def test_redis_roundtrip_preserves_source():
    redis = _Redis()
    ok, reason = await steer.push_steer("s1", "再查一遍日志", source="watchdog", redis=redis)
    assert (ok, reason) == (True, "")

    # 清掉本地队列 = 模拟"消费发生在另一个副本上"，只能读 Redis。
    steer.clear_steer_local("s1")
    assert await steer.drain_steer("s1", redis=redis) == [("再查一遍日志", "watchdog")]


@pytest.mark.asyncio
async def test_local_drain_keeps_order_and_per_item_source():
    redis = _Redis()
    await steer.push_steer("s2", "第一条", source="user", redis=redis)
    await steer.push_steer("s2", "第二条", source="watchdog", redis=redis)

    assert await steer.drain_steer("s2", redis=redis) == [
        ("第一条", "user"),
        ("第二条", "watchdog"),
    ]


@pytest.mark.asyncio
async def test_default_source_is_user():
    """调用方没显式给来源时按用户指令处理 —— Go 侧 `source` 缺省即此。"""
    redis = _Redis()
    await steer.push_steer("s3", "照这个方向改", redis=redis)

    assert await steer.drain_steer("s3", redis=redis) == [("照这个方向改", "user")]
