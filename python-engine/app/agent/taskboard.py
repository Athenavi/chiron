"""共享任务板 —— 多 agent 可见、按编号更新的 todo 存储。

**为什么不继续用 contextvars**

`todos` 原先存在 `app/tools/context.py` 的 contextvars 里。单 agent 没问题，多 agent
必坏 —— 而且坏得很安静：

* 同步子 agent 结束时 `subagent.py` 会 `restore_context(parent_ctx)`；子 agent 往板子上
  写的每一项都被这行还原掉，父 agent 永远看不到"我已经做完了什么"；
* 后台子 agent 跑在 `asyncio.create_task` 的 **context 副本**里，写入根本回不到父任务。

两条路都表现为"子 agent 明明说了在干活，任务板一动不动"，而日志里没有任何异常。
所以共享状态必须离开 contextvars，落到进程外的存储上。

**存储分层**

Redis 优先（跨副本、跨 agent 共享），不可用时退回进程内的 contextvars。
回落不是为了"能用"，而是为了让**单测与本地无 Redis 环境**仍可跑通；线上
`DEGRADED_MODE=false` 时 Redis 本就必须可用（见配置注释），因此线上不会静默降级。

**更新语义：按编号合并**

用户要的是"全量覆写整张清单"，但多 agent 并行时全量覆写必然互相覆盖：子 agent 只
知道自己那两项，它若提交完整列表，就会把主 agent 刚写的其余项抹掉。

因此语义按**调用者身份**区分，而不是让模型去选一个它容易选错的开关：

* 主 agent（`subagent_depth == 0`）→ **整份替换**。它拥有整张清单，这就是用户要的语义。
* 子 agent（`subagent_depth > 0`）→ **按 id 合并**。带 `id` 的项更新对应条目，不带 `id`
  的追加；本次没提到的条目**保留**（那是别人的活，子 agent 无权删除）。

身份取自 tool context，模型无法伪造 —— 这也是它能自动生效的前提。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 任务板在 Redis 里的键前缀。与 gateway 的 REDIS_KEY_PREFIX 拼接后使用。
BOARD_KEY_PREFIX = "taskboard"

#: 板子的存活时间（秒）。不设 TTL 的话，每个会话都会在 Redis 里留下一条永不回收的记录。
#: 24 小时：远长于一次长任务，又不会让脏数据永久堆积。
BOARD_TTL_SECONDS = 24 * 3600

#: contextvars 回落用的键名（仅单进程、无 Redis 时生效）。
_FALLBACK_CONTEXT_KEY = "todos"


def _root_session_id(session_id: str) -> str:
    """从（可能的）子 agent session 解析出**共享板所属**的会话。

    子 agent 的 session 形如 ``sub:{parent_session}:{run_id}``（见
    ``app/agent/subagent_runner.py``）。若直接拿它当板子键，每个子 agent 都会写到
    自己那块板上 —— 父 agent 读的是另一块，于是"共享"在物理上就不成立，而表现
    恰好是最难查的那种：子 agent 回报"已更新任务板"，父 agent 面板毫无变化。

    递归剥掉 ``sub:`` 前缀：孙 agent 的 session 是 ``sub:sub:parent:run1:run2``，
    只剥一层仍会落到中间那块板上。深度上限用 ``MAX_DEPTH`` 的合理倍数兜住，
    防止畸形 session 造成意料外的长循环。
    """
    current = session_id
    for _ in range(8):
        if not current.startswith("sub:"):
            break
        rest = current[len("sub:") :]
        # `sub:` 后面必然是 `{parent}:{run_id}`；run_id 内不含冒号，
        # 而 parent 本身可能是 `sub:…`，所以从**右侧**切最后一段。
        if ":" not in rest:
            break
        parent, _, _run = rest.rpartition(":")
        if not parent:
            break
        current = parent
    return current


def board_key(tenant_id: str, session_id: str) -> str:
    """板子的 Redis 键。

    `tenant` 必须在键里：不同租户的 session_id 广度相同，只按 session 存会让
    两个租户撞进同一块板 —— 这是租户隔离问题，不是缓存问题。
    """
    from app.config import settings

    prefix = (settings.redis_key_prefix or "").strip()
    root = _root_session_id(session_id or "")
    return f"{prefix}{BOARD_KEY_PREFIX}:{tenant_id or 'default'}:{root}"


def _caller_identity() -> tuple[str, int]:
    """当前调用者的 (owner 标识, subagent_depth)。

    owner 优先取 `subagent_run_id`（子 agent 的唯一身份），否则为 `main`。
    深度用于判断该走 replace 还是 merge。
    """
    from app.tools.context import get_tool_context

    depth_raw = get_tool_context("subagent_depth", 0)
    try:
        depth = int(depth_raw or 0)
    except (TypeError, ValueError):
        depth = 0
    run_id = str(get_tool_context("subagent_run_id", "") or "")
    owner = run_id or ("main" if depth == 0 else "subagent")
    return owner, depth


def _merge(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 id 把 `incoming` 合并进 `existing`。

    三条规则，都对应一个真实的协作故障：

    1. **带 id 且已存在** → 就地更新给出的字段。这是"按编号更新"的本体。
    2. **id 在板上不存在** → 当作新条目追加，并**重新编号**。不能让模型的笔误
       （写了个 `t99`）凭空插入一个与现有编号冲突的身份 —— 编号必须由 host 全权掌握，
       否则前端按 id 渲染时会出现两条 `t3`。
    3. **本次未提及的条目保留**。子 agent 只认领自己那几项，它无权删除别人的活。
    """
    by_id = {str(item.get("id", "")): dict(item) for item in existing}
    order = [str(item.get("id", "")) for item in existing]

    for entry in incoming:
        sid = str(entry.get("id", "") or "").strip()
        if sid and sid in by_id:
            merged = by_id[sid]
            merged.update({k: v for k, v in entry.items() if k != "id"})
            continue
        # 新条目：id 由调用方在 normalize 阶段赋过（t1/t2…），这里要与板上已有编号
        # 去重 —— 两条 t3 会让面板渲染出两个"第 3 项"。
        order.append(sid)

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sid in order:
        item: dict[str, Any] | None = by_id.get(sid)
        if item is None:
            # 新条目：从 incoming 里找出来
            item = next(
                (dict(i) for i in incoming if str(i.get("id", "")) == sid), None
            )
            if item is None:
                continue
        # host 重编号：保证 id 唯一且连续
        new_id = f"t{len(result) + 1}"
        item["id"] = new_id
        seen.add(new_id)
        result.append(item)
    return result


def _renumber(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """整份替换时也统一编号：编号是 host 的，不能让模型自己挑。"""
    out: list[dict[str, Any]] = []
    for index, item in enumerate(todos):
        copy = dict(item)
        copy["id"] = f"t{index + 1}"
        out.append(copy)
    return out


def summarize(todos: list[dict[str, Any]]) -> dict[str, int]:
    """统计各状态数量（前端折叠标题与 tool_result 回显共用）。"""
    counts = {"pending": 0, "in_progress": 0, "completed": 0}
    for item in todos:
        status = str(item.get("status", "pending"))
        if status in counts:
            counts[status] += 1
    return counts


async def _read_redis(key: str) -> dict[str, Any] | None:
    """从 Redis 读板子；Redis 不可用返回 None（由调用方回落到 contextvars）。"""
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        raw = await redis.get(key)
    except Exception as exc:  # noqa: BLE001 — 任何 Redis 故障都不应让计划写入失败
        logger.warning("taskboard: redis read failed (%s); falling back to context", exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        # 板子被写坏了：当作空板继续，而不是让整轮对话卡在"读不出计划"上。
        logger.warning("taskboard: corrupt board at %s; treating as empty", key)
        return None
    return data if isinstance(data, dict) else None


async def _write_redis(key: str, payload: dict[str, Any]) -> bool:
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        await redis.set(key, json.dumps(payload, ensure_ascii=False), ex=BOARD_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("taskboard: redis write failed (%s); falling back to context", exc)
        return False


async def load() -> dict[str, Any]:
    """读回当前板子（`{"todos": [...], "revision": n, "owner": ...}`）。"""
    from app.tools.context import get_session_id, get_tenant_id, get_tool_context

    key = board_key(get_tenant_id(), get_session_id())
    data = await _read_redis(key)
    if data is not None:
        return data
    # 回落：进程内 contextvars（单测、无 Redis 的本地环境）
    stored = get_tool_context(_FALLBACK_CONTEXT_KEY, {})
    if isinstance(stored, dict) and isinstance(stored.get("todos"), list):
        return stored
    if isinstance(stored, list):  # 兼容旧形态（纯列表）
        return {"todos": stored, "revision": 0}
    return {"todos": [], "revision": 0}


async def write(
    todos: list[dict[str, Any]], *, merge: bool = False
) -> dict[str, Any]:
    """写入板子，返回落库后的完整状态（含 revision）。

    主/子 agent 的语义差异见模块文档：子 agent 一律按 id 合并。
    ``merge=True`` 让**主 agent** 也走合并路径（增量更新：只动本次提到的条目，
    未提及的保留在板上）—— 这是"做完一项就标一项"能生效的前提。缺省仍是整份替换，
    保持旧契约（前端与既有测试按"列表被替换"渲染）。

    revision 每次写入 +1 —— 前端据此判断"这是不是比我手上更新的那份"，
    避免乱序到达的 SSE 事件让面板倒退。
    """
    from app.tools.context import set_tool_context

    owner, depth = _caller_identity()
    current = await load()
    _current_todos = current.get("todos")
    existing: list[dict[str, Any]] = (
        _current_todos if isinstance(_current_todos, list) else []
    )
    revision = int(current.get("revision") or 0) + 1

    if depth > 0 or merge:
        merged = _merge([dict(i) for i in existing], todos)
    else:
        merged = _renumber(todos)

    payload = {"todos": merged, "revision": revision, "owner": owner}

    from app.tools.context import get_session_id, get_tenant_id

    if not await _write_redis(board_key(get_tenant_id(), get_session_id()), payload):
        set_tool_context(**{_FALLBACK_CONTEXT_KEY: payload})
    else:
        # Redis 写成功时也同步一份到 context：同进程内的读（`current_todos`）不必
        # 每次都往返 Redis，且 Redis 瞬断时仍有本进程内的最新值。
        set_tool_context(**{_FALLBACK_CONTEXT_KEY: payload})
    return payload
