"""任务线账（run 级预算的第二道闸门）—— M15b，账本单位是 0021 的 ``root_run_id``。

**要拦的是什么**（论文 §5.2）：一条任务线可以被反复续跑，每次都是全新 run_id，而 per-run
预算只认自己这一 run。于是"续跑次数 × 每 run 额度"就是实际花销，而实际花销没有任何上限。

**口径**（只有一个加法，写在 ``LineState.charged`` 里）::

    charged(line) = settled(line)        # 已终态 run 的 token 之和 —— 权威：subagent_runs 求和
                  + reserved(line)       # **其它**在途 run 的预留（Redis，到点自动回收）
                  + own                  # 当前 run 自己已花（它的行还没终态，不在 settled 里）

    越界 ⇔ cap > 0 且 charged >= cap（轴名 ``line_tokens``）；或 ``paused``（轴名 ``line_paused``）

**为什么 ``settled`` 不抄进表里**：那张表（迁移 0022）只存控制态（cap / paused）。累计一旦
有了第二份，它就一定会和明细对不上（写 run 行与加累计是两条语句，中间崩溃、补偿、人工修数据
都能把它们岔开）—— 预算账上最坏的一类静默。Redis 里的那个键只是**镜像**：缺失就从 PG 求和
回填，TTL 60s，权威永远是求和。

**为什么预留放 Redis 而不是表里**：预留是"到点就该消失"的协调态。放 PG 就得配回收任务 ——
崩溃的 run 会把预留永久留在条线上，那条线从此被占住、新派发一律被拒，比没有闸门更糟。Redis
sorted set 以**到期时刻为 score**、读时先按 score 清，崩溃留下的预留到点自己消失；正常收尾
由 :meth:`LineLedger.settle` 显式摘掉。Redis 整个不可用时退回**只结算不预留**（最坏后果是
同一条线上并发的几个 run 各自按剩余量起算，溢出至多半 run —— 而 per-run 预算与租户预算仍在）。

**崩溃留下的预留怎么提前收回**（M17）：只靠 score 的话，一条被死实例占住的线要空等
``RESERVE_TTL_SECONDS``（1h）才恢复。现在 member 第三段带**持有者实例 id**
（``member<TAB>tokens<TAB>holder``），读路径对每条线每隔 ``SWEEP_COOLDOWN_SECONDS`` 拿
``app/instance_liveness.py`` 的存活键核对一次持有者：实例已经没了 ⇒ 它的预留当场作废。对
``SIGKILL`` 也成立 —— 判据是"心跳键不见了"，不是"进程自己告诉我它要走了"。优雅退出那一路
另有主动释放：:meth:`LineLedger.drain` 在 lifespan 关闭时按 holder 摘掉本进程还挂着的全部
预留，不必等 TTL（参照 etcd ``Session.Close()`` 撤销挂在其下的键、Kafka 的 ``LeaveGroup``）。

清扫的**前提**是本进程自己也在写存活键（``instance_liveness.participates()``）：不在协议里时
"读不到别人的存活键"根本不构成证据，照删等于把全站的在途预留扫空 —— 少算（上限失效）正是这条
账最坏的一类静默。滚动升级期间没有第三段的旧 member 不参与清扫，仍由 score 收口。

**在途预留为什么会过时**（M18）：预留只在派发时写一次，值是"这一 run 的 per-run token 上限"
—— 而那是一个**猜**：上限已知时它是个确定上界（一个 run 不可能花超自己的上限），未知时
（token 轴为 0 = 不限）**干脆不写**。于是同一条线上并发的其它 run 永远只看到一个静态数字，
这个 run 真实花了多少只有它自己知道；再叠加上 ``RESERVE_TTL_SECONDS`` 到点蒸发（跑满 1h 的
合法 run 会中途失去预留），"在途那一段"就是论文 §5.2 ② 那笔系统性少算的主要来源。

现在预留是**覆盖式重述**的：:meth:`LineLedger.project` 把这一 run 的预留改写成
``已花 + 投影余量``，投影 = 观测速率 × ``PROJECTION_LOOKAHEAD_SECONDS``（速率分母最小
``PROJECTION_MIN_SAMPLE_SECONDS``，否则一个 1 秒的样本能外推出 60 倍），上限夹在 per-run
token 上限（已知时），下限不许低于已花。**没有样本就不预测**：``spent`` 为 0 时一个字都不写，
派发时那份 seed 原样保留 —— 宁可沿用"确定上界"，也不编一个数。参照 K8s APF 的"按观测到的
demand 周期性重算动态并发上限（floor / ceiling 夹住）"与 Stripe 预聚合的"同一窗口后写覆盖前写"。
重述顺带把 score 推到 ``now + RESERVE_TTL`` ⇒ **活着的** run 不再因为跑得久而丢掉预留，而死了的
由 M17 的清扫当场摘掉（两件事合起来才自洽：刷新让长 run 不被误当成幽灵，清扫让幽灵不被刷新
当成活人）。

⚠ **代价要说清**：派发时的 seed 是确定上界，重述之后它变成估计。一个"花得比上限慢"的 run
对同线其它 run 的可见量会**下降** ⇒ 同一条线允许的并发在途变多。这是"双向覆盖"相对"只升不降"
的取舍（换到的是在途那一段不再是盲区）。因此默认 **shadow**（``SUBAGENT_LINE_PROJECTION``）：
只算、只打点，**不写、不改判定**；看过 ``subagent_line_projection_*`` 的分布再按部署切 enforce。

**fail-open**：任何一步问不出来（没有租户、PG 不可用、列还没迁移）都返回 ``None`` / 放行。
线账是第二道闸门，不是唯一一道；反过来 fail-closed 会让一次 PG 抖动停掉全站子 Agent，代价与
收益不符 —— 与 ``internal/api/ent_quota_enforce.go`` 的 ``consume()`` 同一取向（那里也是
"计数失败 ⇒ fail-open + warn"）。``paused`` 不同：它是**人显式按下的暂停键**，读得到就必须停。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from app.observability.metrics import (
    SESSION_LINE_GATE,
    SUBAGENT_LINE_GATE,
    SUBAGENT_LINE_PROJECTION,
    SUBAGENT_LINE_PROJECTION_TOKENS,
    SUBAGENT_LINE_SWEEP,
)
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

#: 一条线的默认 token 上限。取值参照 per-run 默认（子 Agent 200k）与"正常一个 run 约
#: 2k–20k token"的实测：2M ≈ 100–1000 个 run 的全程用量 —— 只拦"明显失控"（无人值守
#: 续跑循环、被 prompt 注入驱动的连环委派），不会碰掉正常任务。
DEFAULT_LINE_MAX_TOKENS = 2_000_000
#: ``run_lineage_ledger.cap_tokens`` 的显式"这条线不限"。与 0（= 跟随默认）分开：
#: 默认值改了不该悄悄关掉一条被明确放行的大线。
CAP_UNLIMITED = -1

#: ``pause_reason`` 的取值：闸门因预算耗尽自己按下的暂停。写进库、也写进待办的 request，
#: 让人一眼分清"这条线是被钱停的"还是"被谁手动停的"（后者目前还没有入口，但这一列的语义
#: 从第一天起就按可区开来设计，免得以后要靠时间戳猜）。
PAUSE_REASON_BUDGET = "budget_exhausted"

#: 人工提额允许写进去的上限上界。它只用来挡住"手滑多打两个 0"，不是产品语义 ——
#: 真要一条不限的线，由运维把 ``cap_tokens`` 直接写成 ``-1``（API 故意不开这个口子：
#: "把上限关掉"和"这次多给一点"是两个决定，前者不该由一张待办的一次答复完成）。
MAX_CAP_TOKENS = 10**12

#: 投影窗口（秒）：``重述值 = 已花 + 观测速率 × 这一条``。
#: 判据是"必须明显宽于重述的节拍"—— 节拍是每一次带用量的模型调用（≈一个回合，实测 10s
#: 量级），取 120s ⇒ 两次重述之间，这条预留不会先于真实花销见底。
PROJECTION_LOOKAHEAD_SECONDS = 120
#: 速率的分母下限（秒）。没有它，一个 1 秒的样本就能把 5k token 外推成 60 倍 ——
#: 而猜大了会误拦同一条线上的其它 run（预留是别人的账）。
PROJECTION_MIN_SAMPLE_SECONDS = 30
#: ``SUBAGENT_LINE_PROJECTION`` 的取值与默认。默认 shadow 的理由写在模块头"代价要说清"那段。
PROJECTION_MODES = ("off", "shadow", "enforce")
DEFAULT_PROJECTION_MODE = "shadow"

#: M26 会话线闸门（``SESSION_LINE_GATE``）的取值与默认。
#:
#: 与 ``SUBAGENT_LINE_PROJECTION`` 同一套三档（off / shadow / enforce），默认同样是 **shadow**：
#: 这条闸门要改变的是**主 run**（每一条消息）的行为，一旦判错就是"用户的消息跑不动了"。
#: 先只算、只打点，看过 ``session_line_gate_total`` 的分布再按部署切 enforce。
SESSION_LINE_MODES = ("off", "shadow", "enforce")
DEFAULT_SESSION_LINE_MODE = "shadow"


def _rowcount(result: Any) -> int:
    """从 asyncpg 的 ``UPDATE n`` / ``INSERT 0 n`` 状态串里取行数。"""
    parts = str(result or "").split()
    if len(parts) >= 2 and parts[0] in ("UPDATE", "DELETE"):
        try:
            return int(parts[1])
        except ValueError:
            return 0
    if len(parts) == 3 and parts[0] == "INSERT":
        try:
            return int(parts[2])
        except ValueError:
            return 0
    return 0

#: settled 镜像的 TTL。它只用于吸收 fan-out 时的重复聚合，故意取短：漂移上界就是这段时间
#: 内**丢掉**的终态写入（镜像只在键还活着时 INCRBY；键没了 ⇒ 下一次读走 PG 求和）。
USED_TTL_SECONDS = 60
#: 单条在途预留的自然到期。必须宽过"一次 run 可能跑多久"（后台 run 的 wall 默认不限，
#: 由注册表看门狗收口），又要短到崩溃留下的预留不会长期占住这条线。
RESERVE_TTL_SECONDS = 3600
#: 死实例预留清扫的最小间隔（**按线**计）。它是"读路径顺带做对账"的节流：一小时的幽灵预留
#: 缩成最多 30 秒，代价是每条**有在途预留**的线每 30 秒多一次 ``ZRANGE`` + 几次 ``EXISTS``。
SWEEP_COOLDOWN_SECONDS = 30
#: 本进程在途预留登记簿的上界。正常尺寸 = 并发 run 数（个位数），`settle` 会逐条摘掉；
#: 这一条只挡"收尾路径被跳过"时的无界增长（长时间运行的进程，每条 50B 也迟早变成显性泄漏）。
MAX_TRACKED_HANDLES = 4096

#: 线账读过的控制态行（迁移 0022）。``tenant_id`` 是**必填**过滤：闸门的调用形状是
#: "给我一个 run id 我就能问这条线的额度"，不按租户收就是跨租户读数。
LEDGER_SELECT_SQL = (
    "SELECT cap_tokens, paused, pause_reason FROM run_lineage_ledger "
    "WHERE root_run_id = $1 AND tenant_id = $2"
)

#: 保证那一行存在（没被设过的线在表里本来就没有行）。`DO NOTHING`：已有行**一律不动**，
#: 要改的东西全部交给下面那条 UPDATE —— 两条各司其职，比一条带六个 COALESCE 的
#: `ON CONFLICT DO UPDATE` 好读，也好让"归属不符"只可能出现在 UPDATE 的行数上。
LEDGER_ENSURE_SQL = (
    "INSERT INTO run_lineage_ledger (root_run_id, tenant_id) VALUES ($1, $2) "
    "ON CONFLICT (root_run_id) DO NOTHING"
)

#: 控制态的**增量**写：`NULL` = 这一列不改。`pause_reason` 用显式 `::text` 区分
#: "没带这个字段"（NULL，保持原值）与"要清掉它"（空串 ⇒ 落 NULL）。
#:
#: `AND tenant_id = $2` 是这条写路径唯一的跨租户护栏（主键只有 `root_run_id`）：归属不符时
#: UPDATE 匹配 0 行 ⇒ 返回 False，调用方如实报错，而不会把别人的线上限改掉。
LEDGER_UPDATE_SQL = """
UPDATE run_lineage_ledger
   SET cap_tokens   = COALESCE($3, cap_tokens),
       paused       = COALESCE($4, paused),
       pause_reason = CASE WHEN $5::text IS NULL THEN pause_reason ELSE NULLIF($5, '')::text END,
       updated_by   = COALESCE(NULLIF($6, ''), updated_by),
       updated_at   = now()
 WHERE root_run_id = $1
   AND tenant_id = $2
"""

_TAB = "\t"

# 预留表的一个原子操作：可选删除 / 可选新增 / 清过期 / 求和（排除 exclude 那个 run ——
# 它的花销由调用方按**实际** token 计，不能再算一遍预留）。
# KEYS[1]=预留 zset；ARGV: 1=now(unix s) 2=add member 3=add expiry 4=del member
# 5=exclude run_id 6=ttl s。member 形状 `run_id<TAB>tokens<TAB>holder`，score = 到期时刻。
#
# 取 tokens 时必须**只取第二段**（``rest`` 到下一个 TAB 为止）：直接 ``tonumber(sub(p+1))``
# 在三段 member 上会得到 nil ⇒ 该条预留按 0 计 —— 上限静默失效，而且只有加了 holder 之后
# 才会出现，是"看起来还在工作"的最坏形状。第三段缺失（滚动升级期间的老实例）照常计。
_RSVD_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local del = ARGV[4]
if del ~= '' then redis.call('ZREM', key, del) end
local add = ARGV[2]
if add ~= '' then redis.call('ZADD', key, tonumber(ARGV[3]), add) end
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
local skip = ARGV[5]
local total = 0
local members = redis.call('ZRANGE', key, 0, -1)
for i = 1, #members do
  local m = members[i]
  local p = string.find(m, '\\t', 1, true)
  if p then
    local who = string.sub(m, 1, p - 1)
    if who ~= skip then
      local rest = string.sub(m, p + 1)
      local q = string.find(rest, '\\t', 1, true)
      if q then rest = string.sub(rest, 1, q - 1) end
      total = total + (tonumber(rest) or 0)
    end
  end
end
if #members > 0 then
  redis.call('EXPIRE', key, tonumber(ARGV[6]))
else
  redis.call('DEL', key)
end
return total
"""

# 按**持有者**摘预留：清扫（某实例的存活键没了）与 drain（本进程优雅退出）共用这一条规则。
# 一个 EVAL 只碰一个键（与上面同样口径：跨键会在 Cluster 下 CROSSSLOT，见 KeyRing V2 的教训），
# 所以存活键由 Python 侧读，这里只按传进来的 holder 匹配第三段。
# 没有第三段的旧 member **不匹配**（``second`` 为 nil）—— 老实例的预留只能由 score 收口，
# 这条脚本在滚动升级期间不会误删还在跑的东西。
_RSVD_DROP_HOLDER_LUA = """
local key = KEYS[1]
local holder = ARGV[1]
local removed = 0
local members = redis.call('ZRANGE', key, 0, -1)
for i = 1, #members do
  local m = members[i]
  local first = string.find(m, '\\t', 1, true)
  if first then
    local rest = string.sub(m, first + 1)
    local second = string.find(rest, '\\t', 1, true)
    if second and string.sub(rest, second + 1) == holder then
      removed = removed + redis.call('ZREM', key, m)
    end
  end
end
if redis.call('ZCARD', key) == 0 then redis.call('DEL', key) end
return removed
"""

# 预留的**覆盖式重述**（M18）：把这一 run 在 Redis 里的那条（些）预留摘掉，换上
# `member<TAB>新 tokens<TAB>持有者`，并把 score 推到新的到期时刻。
#
# 按**第一段**匹配而不是整条 member：重述本来就是要换掉那条 member（tokens 就编在名字里），
# 拿旧串去 `ZREM` 只能摘掉"自己写的那一版"，一旦中途重述过就会留下几条同 run 的预留并存 ——
# 求和把同一段花销数好几遍（**多算**，会把一条正常的线判死）。所以这里把所有 `who` 命中的
# 都摘掉、把它们的 tokens 相加返回（"上一条是多少"只用来报方向，判定不读它）。
#
# 一次 EVAL 只碰一个键（KeyRing V2 的 CROSSSLOT 教训同上），且顺带做完过期清理与 EXPIRE，
# 与 `_RSVD_LUA` 的收尾口径一致：空了就 DEL，不留"存在但为 0"的键。
_RSVD_RESTATE_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local who = ARGV[2]
local member = ARGV[3]
local prior = 0
local members = redis.call('ZRANGE', key, 0, -1)
for i = 1, #members do
  local m = members[i]
  local p = string.find(m, '\\t', 1, true)
  if p and string.sub(m, 1, p - 1) == who then
    local rest = string.sub(m, p + 1)
    local q = string.find(rest, '\\t', 1, true)
    if q then rest = string.sub(rest, 1, q - 1) end
    prior = prior + (tonumber(rest) or 0)
    redis.call('ZREM', key, m)
  end
end
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
redis.call('ZADD', key, tonumber(ARGV[4]), member)
redis.call('EXPIRE', key, tonumber(ARGV[5]))
return prior
"""

# 镜像缺失时的回填：**只在键不存在时**写（NX）。已经存在的值可能是并发 run 刚 INCRBY 过的，
# 覆盖它会把那段实际用量抹掉（**少算**，是这条线最坏的一侧）。
_USED_BACKFILL_LUA = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', tonumber(ARGV[2])) then return 1 end
return 0
"""

# 终态写入后的镜像累加：键不在了就**什么都不做**（那一步的权威值已经在 PG 的那一行里，
# 下一次读会直接从求和回填出来）。这里不加回一个已消失的键，否则会绕过权威口径。
_USED_BUMP_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  local n = redis.call('INCRBY', KEYS[1], ARGV[1])
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
  return n
end
return -1
"""


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def default_cap() -> int:
    """线级默认上限（``SUBAGENT_LINE_MAX_TOKENS``）。0 = 该轴不限。"""
    return _env_int("SUBAGENT_LINE_MAX_TOKENS", DEFAULT_LINE_MAX_TOKENS)


def projection_mode() -> str:
    """预留重述的开关（``SUBAGENT_LINE_PROJECTION``）：``off`` / ``shadow`` / ``enforce``。

    默认 **shadow**：算、打点，但不写 Redis、不改判定。理由见模块头"代价要说清"那一段 ——
    重述把派发时那份"确定上界"换成估计，一个花得比上限慢的 run 会让同线的其它 run 看到
    **更小**的预留，于是允许的并发在途变多。这个开关是给运维看过
    ``subagent_line_projection_tokens`` 的分布之后、**按部署**切 enforce 用的。
    取不到或不认识的值一律回落到默认（与 ``_env_int`` 同一口径：配置写错不该改变判定）。
    """
    raw = (os.getenv("SUBAGENT_LINE_PROJECTION") or "").strip().lower()
    return raw if raw in PROJECTION_MODES else DEFAULT_PROJECTION_MODE


def session_line_mode() -> str:
    """会话线闸门的开关（``SESSION_LINE_GATE``）：``off`` / ``shadow`` / ``enforce``。

    默认 **shadow**：算、打点、**不改判定**。取不到或不认识的值一律回落到默认 —— 与
    ``projection_mode`` 同一口径：配置写错不该改变判定。
    """
    raw = (os.getenv("SESSION_LINE_GATE") or "").strip().lower()
    return raw if raw in SESSION_LINE_MODES else DEFAULT_SESSION_LINE_MODE


def _usable_root(root: str) -> bool:
    """run id 会进 Redis 键名与 Lua 参数 —— 只接受自己的 id 字符集。

    预留的 member 用 TAB 分隔，一个带 TAB/空白/控制字符的 id 会把它拼出来的 member 变成
    另一条记录；这不是"理论上脏数据"，而是键空间与账目的注入面。
    """
    if not root or len(root) > 64:
        return False
    return all(c.isalnum() or c in "-_." for c in root)


def _holder_of(member: str) -> str:
    """预留 member 的第三段（持有者实例 id）；老实例写的两段 member 返回空串。

    与 ``_RSVD_LUA`` 里的字段顺序一一对应 —— Python 侧只用来**挑出要问谁**，删除动作全在脚本里，
    两处对字段的解释必须一致，所以字段口径只在这一份脚本 + 这一个函数里定义。
    """
    parts = member.split(_TAB)
    return parts[2] if len(parts) >= 3 else ""


def projected_tokens(*, spent: int, elapsed_seconds: float, per_run_cap: int) -> int:
    """这一 run 该让同线其它 run 看见的预留 = 已花 + 观测速率 × 投影窗口。

    返回 ``0`` = **不预测**（还没有样本）：调用方据此什么都不写，派发时那份 seed 原样保留 ——
    宁可沿用"确定上界"，也不编一个数。

    两道夹紧都是方向性的：上限夹在 per-run token 上限（已知时 —— 一个 run 不可能花超自己的
    上限，超出那部分不是"这条线多出来的风险"），下限不许低于已花（重述绝不能把**已经花掉的**
    钱变得看不见）。per_run_cap 为 0（不限）时只有下限，投出去的是一个纯估计。
    """
    spent = max(0, int(spent or 0))
    if spent <= 0:
        return 0
    sample = max(float(elapsed_seconds or 0.0), float(PROJECTION_MIN_SAMPLE_SECONDS))
    projected = spent + int(spent / sample * PROJECTION_LOOKAHEAD_SECONDS)
    if per_run_cap > 0:
        projected = min(projected, int(per_run_cap))
    return max(projected, spent)


@dataclass(frozen=True)
class LineState:
    """一条任务线在被问到的那一刻的账目。"""

    root: str
    tenant_id: str
    #: 生效上限（已解析过默认值）：``0`` = 该轴不限。
    cap: int
    settled: int
    reserved: int
    paused: bool
    pause_reason: str

    def charged(self, own_tokens: int) -> int:
        return self.settled + self.reserved + max(0, int(own_tokens or 0))

    def violation(self, own_tokens: int) -> str:
        """越界轴名（``line_paused`` / ``line_tokens``）；没越界返回空串。

        判定顺序固定：先 ``paused`` —— 它是人显式按下的暂停键，与"数算到了上限"是两件事，
        上层要据此决定"是等人提额还是直接判失败"。
        """
        if self.paused:
            return "line_paused"
        if self.cap > 0 and self.charged(own_tokens) >= self.cap:
            return "line_tokens"
        return ""

    def describe(self) -> str:
        cap = self.cap if self.cap > 0 else "unlimited"
        return (
            f"root={self.root} settled={self.settled} reserved={self.reserved} "
            f"cap={cap} paused={int(self.paused)}{(':' + self.pause_reason) if self.pause_reason else ''}"
        )


def _handle_for(member: str, tokens: int) -> str:
    """预留 member 的字段协议只有一个写法：``member<TAB>tokens<TAB>持有者``。

    ``reserve`` 与 ``project`` 都从这里拼串 —— 两处各拼一遍迟早和 ``_holder_of`` / Lua 脚本
    对字段的解释漂移（而那条漂移的方向是"预留按 0 计"= 上限静默失效）。

    持有者来自部署配置（``INSTANCE_ID`` / ``POD_NAME``），**不保证**符合 id 字符集：一个带 TAB
    的值会把 member 的字段协议拆坏（``_holder_of`` 读出前缀 ⇒ 问错存活键、按错 holder 删，
    本实例在跑的预留会被自己扫掉）。不署名的退化是安全的：照旧计入求和、只由 score 收口，
    与滚动升级期间老实例写的两段 member 完全同形。
    """
    from app.subagent.affinity import cached_instance_id

    holder = cached_instance_id()
    if not _usable_root(holder):
        holder = ""
    return f"{member}{_TAB}{tokens}{_TAB}{holder}" if holder else f"{member}{_TAB}{tokens}"


class LineLedger:
    """线账的读写。不持有 run 级状态 ⇒ 进程内共享一个实例（见 :func:`get_line_ledger`）。"""

    def __init__(self, *, pool: Any = None, redis: Any = None, store: Any = None) -> None:
        self._pool = pool
        self._redis = redis
        self._store = store
        #: 同类失败只告警一次：闸门每轮都会被问一次，Redis 抖动时这会变成日志洪水。
        self._warned: set[str] = set()
        #: 本进程还挂着的预留：``handle → root``。只服务一件事 —— 优雅退出时的
        #: :meth:`drain`（要知道该去哪几条线上摘自己的预留）。收尾即出账，正常尺寸 = 并发 run 数。
        self._held: dict[str, str] = {}
        #: 每条线**下一次**允许清扫的时刻（``time.monotonic`` 口径，不受改时钟影响）。
        self._swept_at: dict[str, float] = {}

    # ── 连接 ──

    async def _redis_conn(self) -> Any:
        if self._redis is not None:
            return self._redis
        from app.redis_client import get_redis

        self._redis = await get_redis()
        return self._redis

    def _db_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from app.db import get_pool

        self._pool = get_pool()
        return self._pool

    def _run_store(self) -> Any:
        """复用 ``SubagentRunStore`` 的求和口径（``lineage_usage``），别再写一遍 SQL。"""
        if self._store is not None:
            return self._store
        from app.subagent.store import SubagentRunStore

        self._store = SubagentRunStore(self._db_pool())
        return self._store

    def _warn(self, tag: str, msg: str, *args: Any) -> None:
        if tag in self._warned:
            return
        self._warned.add(tag)
        logger.warning(msg, *args)

    # ── 读 ──

    async def state(
        self,
        root: str,
        tenant_id: str,
        *,
        exclude_run_id: str = "",
        scope: str = "delegation",
    ) -> LineState | None:
        """这条线今天的账目。``None`` = **问不出来**（调用方按"没有这条线"放行）。

        ``exclude_run_id`` 是"这笔查询替谁算"：它自己的预留要从 ``reserved`` 里剔除，
        因为它的花销按**实际** token（``own_tokens``）计 —— 两者都算就是双算，
        一条线会提前一个 per-run 预算被判满。
        """
        if not _usable_root(root) or not tenant_id:
            return None
        ctrl = await self._ctrl_row(root, tenant_id)
        if ctrl is None:
            return None
        settled = await self._settled(root, tenant_id, scope=scope)
        if settled is None:
            return None
        cap = ctrl[0] if ctrl[0] != 0 else default_cap()
        if cap == CAP_UNLIMITED:
            cap = 0
        return LineState(
            root=root,
            tenant_id=tenant_id,
            cap=int(cap),
            settled=int(settled),
            reserved=int(await self._reserved(root, exclude_run_id)),
            paused=bool(ctrl[1]),
            pause_reason=str(ctrl[2] or ""),
        )

    async def check(
        self, root: str, tenant_id: str, *, run_id: str = "", own_tokens: int = 0
    ) -> str:
        """闸门：返回越界轴名（``""`` = 放行，含"问不出来"）。"""
        state = await self.state(root, tenant_id, exclude_run_id=run_id)
        if state is None:
            return ""
        axis = state.violation(own_tokens)
        if axis:
            SUBAGENT_LINE_GATE.labels(axis=axis).inc()
            logger.warning("subagent line gate tripped (%s): %s", axis, state.describe())
        return axis

    async def check_session(
        self, session_id: str, tenant_id: str, *, run_id: str = "", own_tokens: int = 0
    ) -> str:
        """M26 会话线闸门：这条**会话**到今天该不该停（返回轴名，``""`` = 放行）。

        与 :meth:`check` 的分工：那条数**委派树**（`subagent_runs` 按 `root_run_id`），这条数
        **主 run 的回合**（`turns` 按 `session_id`）。主 run 不写 `subagent_runs`，所以它此前
        完全不在线账内 —— 而"续跑次数 × 每 run 额度 = 无上限"这句话说的正是主 run 的反复续跑。

        **只判定、不预留**：主 run 是执行体本身，"预留自己的上限"是自指。预留仍只属于被派发出
        去的那一层（子 Agent）；这里的到限处置交给调用方（硬拦 + 挂提额待办）。

        开关（``SESSION_LINE_GATE``）：``off`` 直接放行；``shadow``（默认）算、打点、**不改判定**；
        ``enforce`` 越界即返回轴名。``state`` 为 ``None``（问不出来）一律放行 —— 与 :meth:`check`
        同口径：闸门抖动不该停掉全站。
        """
        mode = session_line_mode()
        if mode == "off":
            SESSION_LINE_GATE.labels(outcome="off", axis="-").inc()
            return ""
        state = await self.state(session_id, tenant_id, exclude_run_id=run_id, scope="session")
        if state is None:
            SESSION_LINE_GATE.labels(outcome="unknown", axis="-").inc()
            return ""
        axis = state.violation(own_tokens)
        if not axis:
            SESSION_LINE_GATE.labels(outcome="ok", axis="-").inc()
            return ""
        outcome = "tripped" if mode == "enforce" else "shadow"
        SESSION_LINE_GATE.labels(outcome=outcome, axis=axis).inc()
        logger.warning("session line gate (%s, mode=%s): %s", axis, mode, state.describe())
        return axis if mode == "enforce" else ""

    async def _ctrl_row(self, root: str, tenant_id: str) -> tuple[int, bool, str] | None:
        """控制态**直接读 PG、不做镜像**。

        这是刻意的：cap/paused 是"人改了就应当生效"的东西。缓存 60s 换来的是
        "提了额这条线还在停"的最坏一分钟，而它只是主键上的一行读 —— 不值得。
        """
        try:
            row = await self._db_pool().fetchrow(LEDGER_SELECT_SQL, root, tenant_id)
        except Exception as exc:  # noqa: BLE001
            self._warn(
                "ctrl",
                "subagent 线账读控制态失败（此后不再重复告警）：请执行迁移 "
                "0022_run_lineage_ledger: %s",
                str(exc)[:160],
            )
            return None
        if row is None:
            # 没有行 = 这条线没被设过 = 跟随默认、未暂停。**不是**"问不出来"。
            return (0, False, "")
        return (int(row["cap_tokens"] or 0), bool(row["paused"]), str(row["pause_reason"] or ""))

    async def _settled(
        self, root: str, tenant_id: str, *, scope: str = "delegation"
    ) -> int | None:
        """**先读镜像，缺了才去 PG 求和** —— 这条快路径存在的全部理由。

        fan-out 时同一条线上多个 run 每轮都要问一次闸门；没有镜像就等于每轮一次
        ``SUM(input_tokens + output_tokens)``（`subagent_runs` 上按 `root_run_id` 的聚合）。
        镜像最多陈旧 ``USED_TTL_SECONDS``，漂移只来自这段时间里**丢掉**的终态写入，方向是
        **少算** —— 与"每轮都求和但会多算"相比，这一侧的代价是一轮 run 的量级。

        回填用 NX：并发里另一个 run 可能刚把键建起来并 INCRBY 过，覆盖它会把那段实际用量抹掉。

        ``scope="session"``（M26 会话线）：账本换成 `turns` 按 `session_id` 的求和 —— 主 run
        的回合用量。**会话线不做 Redis 镜像**：镜像存在的理由是省 fan-out 的 QPS，而主 run 每
        会话只有一条、每轮一次聚合，量级完全不同；为它维护第二份可能漂移的账不划算。
        """
        if scope == "session":
            usage = await self._run_store().session_usage(root, tenant_id)
            return None if usage is None else max(0, int(usage[0]))
        redis = await self._redis_safe()
        if redis is not None:
            mirrored = await self._mirror_get(root)
            if mirrored is not None:
                return mirrored
        usage = await self._run_store().lineage_usage(root, tenant_id)
        if usage is None:
            return None
        tokens = max(0, int(usage[0]))
        if redis is not None:
            try:
                await redis.eval(
                    _USED_BACKFILL_LUA, 1, _used_key(root), str(tokens), str(USED_TTL_SECONDS)
                )
            except Exception as exc:  # noqa: BLE001
                self._warn(
                    "used",
                    "subagent 线账镜像回填失败（此后不再重复告警，按 PG 值继续）: %s",
                    str(exc)[:160],
                )
        return tokens

    async def _mirror_get(self, root: str) -> int | None:
        """读镜像；**None = 没有可用镜像**（键不存在、Redis 抖了、值是脏的）。"""
        redis = await self._redis_safe()
        if redis is None:
            return None
        try:
            raw = await redis.get(_used_key(root))
        except Exception as exc:  # noqa: BLE001
            self._warn(
                "used",
                "subagent 线账镜像读失败（此后不再重复告警，改问 PG）: %s",
                str(exc)[:160],
            )
            return None
        if raw is None:
            return None
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return None

    async def _reserved(self, root: str, exclude_run_id: str, *, sweep: bool = True) -> int:
        """在途预留之和；取不到 ⇒ 0（= 退回"只结算不预留"，方向是**少算**）。

        ``sweep=True`` 时顺带做死实例核对（按线节流）。``total > 0`` 是它的廉价前置：一条线上
        连一个正数预留都没有时，没有什么可收回的，不必再发两次命令。
        """
        total = await self._sum_reserved(root, exclude_run_id)
        if sweep and total > 0 and self._sweep_now(root):
            if await self._sweep_dead_holders(root):
                # 重算走的是**同一个** ``_sum_reserved``：清扫的意义就在于"这次判定不再计入幽灵"，
                # 而 Python 侧再拼一遍求和口径迟早和脚本漂移。
                total = await self._sum_reserved(root, exclude_run_id)
        return total

    async def _sum_reserved(self, root: str, exclude_run_id: str) -> int:
        redis = await self._redis_safe()
        if redis is None or not _usable_root(root):
            return 0
        now = int(time.time())
        try:
            res = await redis.eval(
                _RSVD_LUA,
                1,
                _rsv_key(root),
                str(now),
                "",
                str(now + RESERVE_TTL_SECONDS),
                "",
                exclude_run_id or "",
                str(RESERVE_TTL_SECONDS),
            )
            return int(res or 0)
        except Exception as exc:  # noqa: BLE001
            self._warn("rsv", "subagent 线账预留读失败（此后不再重复告警，按 0 继续）: %s", str(exc)[:160])
            return 0

    def _sweep_now(self, root: str) -> bool:
        """这条线现在该不该做一次存活核对 —— 以及**能不能**核对。

        ``participates()`` 是前置：本进程没在写存活键时，"读不到别人的存活键"不构成证据，照删
        会把全站的在途预留扫空（少算 = 上限静默失效）。这一条判据在``instance_liveness``里，
        不在这里重复实现。
        """
        from app import instance_liveness

        if not instance_liveness.participates():
            return False
        now = time.monotonic()
        if now < self._swept_at.get(root, 0.0):
            return False
        self._swept_at[root] = now + SWEEP_COOLDOWN_SECONDS
        if len(self._swept_at) > MAX_TRACKED_HANDLES:
            self._swept_at = {k: v for k, v in self._swept_at.items() if v > now}
        return True

    async def _sweep_dead_holders(self, root: str) -> int:
        """把持有者已经不存在的预留当场摘掉，返回摘掉的条数。

        两步、且**各自都只碰一个键**：先 ``ZRANGE`` 取成员、由 Python 读存活键，再用一条只操作
        zset 的脚本按 holder 删。存活键绝不能进 ``EVAL`` —— 那正是 KeyRing V2 学到的 CROSSSLOT。
        """
        from app import instance_liveness

        redis = await self._redis_safe()
        if redis is None:
            return 0
        try:
            members = await redis.zrange(_rsv_key(root), 0, -1)
        except Exception as exc:  # noqa: BLE001 - 扫不动就照旧按 TTL 收口
            SUBAGENT_LINE_SWEEP.labels(outcome="failed").inc()
            self._warn("rsvscan", "subagent 线账预留成员扫描失败（此后不再重复告警）: %s", str(exc)[:160])
            return 0
        holders = {h for h in (_holder_of(str(m)) for m in members) if h}
        if not holders:
            return 0  # 全是滚动升级期间写的两段 member：无持有者可依据，不动
        alive = await instance_liveness.holders_alive(holders)
        if not alive:
            return 0
        dead = sorted(h for h, ok in alive.items() if not ok)
        if not dead:
            SUBAGENT_LINE_SWEEP.labels(outcome="scanned").inc()
            return 0
        removed = 0
        for holder in dead:
            try:
                res = await redis.eval(_RSVD_DROP_HOLDER_LUA, 1, _rsv_key(root), holder)
                removed += int(res or 0)
            except Exception as exc:  # noqa: BLE001
                SUBAGENT_LINE_SWEEP.labels(outcome="failed").inc()
                self._warn(
                    "rsvsweep", "subagent 线账死预留清扫失败（此后不再重复告警）: %s", str(exc)[:160]
                )
        if removed:
            SUBAGENT_LINE_SWEEP.labels(outcome="removed").inc(removed)
            logger.warning(
                "subagent line %s: dropped %d reservation(s) held by dead instance(s) %s",
                root, removed, ",".join(dead),
            )
        return removed

    async def _redis_safe(self) -> Any:
        """Redis 可选件：没配 / 连不上都按 ``None``，让线账退回纯 PG 口径。"""
        try:
            return await self._redis_conn()
        except Exception as exc:  # noqa: BLE001
            self._warn("redis", "subagent 线账无 Redis 可用（此后不再重复告警）：预留轴退化，只按已结算量判: %s", str(exc)[:160])
            return None

    # ── 写 ──

    async def reserve(self, root: str, tenant_id: str, member: str, tokens: int) -> str:
        """为这一 run 预留 ``tokens``，返回**句柄**（``""`` = 没预留成）。

        悲观预留（按该 run 的 per-run token 上限）是有意的：它把"同一条线上并发多少个在途
        run"约束成 ``cap / per_run`` 量级。宁可拒绝第 N 个并发派发（M15c 让它变成待办），
        也不要放任 N 个 run 各自按同一份剩余量起算 —— 那是真的会花超。

        member 用 run 的 task id 而不是 run id：闸门那边能按 ``task.id`` 把自己从求和里剔掉，
        不必再往 ``AgentTask`` 上多塞一个"我预留了多少"的字段。

        第三段是**持有者实例 id**（M17）：清扫与 drain 都按它收账。句柄本身就带着三段，所以
        ``settle`` 只要把句柄原样还回来就行，不需要第二个索引。
        """
        tokens = int(tokens or 0)
        # member 与 root 走同一份字符集校验：它同样拼进 TAB 分隔的 member 里，一个带 TAB 的
        # id 会把自己那条记录拆成两段（``who`` 变成前缀、tokens 解析成 0），预留就此失明。
        if tokens <= 0 or not _usable_root(root) or not _usable_root(member) or not tenant_id:
            return ""
        redis = await self._redis_safe()
        if redis is None:
            return ""
        handle = _handle_for(member, tokens)
        now = int(time.time())
        try:
            await redis.eval(
                _RSVD_LUA,
                1,
                _rsv_key(root),
                str(now),
                handle,
                str(now + RESERVE_TTL_SECONDS),
                "",
                member,
                str(RESERVE_TTL_SECONDS),
            )
        except Exception as exc:  # noqa: BLE001
            self._warn("rsvw", "subagent 线账预留写入失败（此后不再重复告警）: %s", str(exc)[:160])
            return ""
        if len(self._held) < MAX_TRACKED_HANDLES:
            self._held[handle] = root
        return handle

    async def project(
        self,
        root: str,
        tenant_id: str,
        *,
        member: str,
        handle: str,
        spent: int,
        elapsed_seconds: float,
        per_run_cap: int = 0,
    ) -> str:
        """M18：把这一 run 的在途预留**覆盖式重述**成 ``已花 + 投影余量``。

        返回值是**后续该用的句柄**：改成新值时是新句柄，什么都没动时原样还回 ``handle``。
        调用方必须把返回值写回去 —— 预留的 tokens 就编在 member 的名字里，重述等于换了一条
        member；继续拿旧句柄去 ``ZREM`` 会摘不掉，那条旧预留会占满一个 ``RESERVE_TTL``
        （**多算**，同线的派发会被误拦）。

        ``handle`` 可以为空：per-run token 轴不限时派发不写 seed（见 :meth:`reserve`），
        那正是"在途那一段完全看不见"的另一半 —— 有样本就照样补一条纯估计的预留上去。

        **返回原句柄 = 这次没改**，五种情形都是：``off``、``shadow``（默认：算、打点、不写）、
        没有样本（``spent`` 为 0 ⇒ 不编数）、键不合字符集、Redis 写失败（沿用旧预留，
        它仍在求和里，方向是**多算** = 安全的一侧）。
        """
        mode = projection_mode()
        if mode == "off":
            SUBAGENT_LINE_PROJECTION.labels(outcome="off").inc()
            return handle
        if not _usable_root(root) or not _usable_root(member) or not tenant_id:
            return handle
        projected = projected_tokens(
            spent=spent, elapsed_seconds=elapsed_seconds, per_run_cap=per_run_cap
        )
        if projected <= 0:
            SUBAGENT_LINE_PROJECTION.labels(outcome="skipped").inc()
            return handle
        SUBAGENT_LINE_PROJECTION_TOKENS.observe(projected)
        if mode != "enforce":
            SUBAGENT_LINE_PROJECTION.labels(outcome="shadow").inc()
            return handle
        redis = await self._redis_safe()
        if redis is None:
            return handle
        new_handle = _handle_for(member, projected)
        now = int(time.time())
        try:
            prior = int(
                await redis.eval(
                    _RSVD_RESTATE_LUA,
                    1,
                    _rsv_key(root),
                    str(now),
                    member,
                    new_handle,
                    str(now + RESERVE_TTL_SECONDS),
                    str(RESERVE_TTL_SECONDS),
                )
                or 0
            )
        except Exception as exc:  # noqa: BLE001
            SUBAGENT_LINE_PROJECTION.labels(outcome="failed").inc()
            self._warn(
                "rsvproj",
                "subagent 线账预留重述失败（此后不再重复告警，沿用派发时那份）: %s",
                str(exc)[:160],
            )
            return handle
        # 登记簿跟着换：drain 按 root 逐线摘，旧句柄留在册上只会让 MAX_TRACKED_HANDLES 先满
        self._held.pop(handle, None)
        if len(self._held) < MAX_TRACKED_HANDLES:
            self._held[new_handle] = root
        direction = (
            "seeded"
            if not handle
            else ("raised" if projected > prior else ("lowered" if projected < prior else "flat"))
        )
        SUBAGENT_LINE_PROJECTION.labels(outcome=direction).inc()
        return new_handle

    async def set_control(
        self,
        root: str,
        tenant_id: str,
        *,
        cap_tokens: int | None = None,
        paused: bool | None = None,
        reason: str | None = None,
        updated_by: str = "",
    ) -> bool:
        """写控制态；``None`` = 那一列不动。返回**是否真的写到了这一行**。

        与读侧相反，这里**不 fail-open**：``False`` 必须让调用方报错。闸门读不到就放行是
        取舍（还有两道预算在），而"人提了额却没写进去、票却答掉了"是纯粹的谎 —— 用户接下来
        看到的是同一道闸门再次拦下同一件事。

        归属不符（那条线属于别的租户）也返回 ``False``，且**不区分**于"写失败"：区分它会让我
        能枚举别人的 ``root_run_id`` 是否存在，而这条 API 本来就不该回答这个问题。
        """
        if not _usable_root(root) or not tenant_id:
            return False
        if cap_tokens is None and paused is None and reason is None and not updated_by:
            return True  # 什么都没要改：不假装写了一次
        try:
            pool = self._db_pool()
            await pool.execute(LEDGER_ENSURE_SQL, root, tenant_id)
            result = await pool.execute(
                LEDGER_UPDATE_SQL,
                root,
                tenant_id,
                None if cap_tokens is None else int(cap_tokens),
                None if paused is None else bool(paused),
                reason,
                updated_by or "",
            )
        except Exception as exc:  # noqa: BLE001 — 写失败如实返回 False，由调用方报错
            self._warn(
                "ctrlw",
                "subagent 线账写控制态失败（此后不再重复告警）：请执行迁移 "
                "0022_run_lineage_ledger: %s",
                str(exc)[:160],
            )
            return False
        return _rowcount(result) > 0

    async def pause_for_budget(self, root: str, tenant_id: str) -> bool:
        """闸门越界 ⇒ 暂停这条线（**不动 cap**：暂停是"停下来等人"，不是"额度变了"）。

        幂等：已经是 paused 的行再写一次没有副作用，所以每个失败段都可以放心调它。
        """
        return await self.set_control(root, tenant_id, paused=True, reason=PAUSE_REASON_BUDGET)

    async def raise_cap_and_resume(
        self, root: str, tenant_id: str, *, cap_tokens: int, updated_by: str
    ) -> bool:
        """人工提额：写新上限**并**清暂停 —— 两者必须同时发生。

        只清暂停不写 cap，下一条线会以同样的数字再次拦下；只写 cap 不清暂停，闸门会一直报
        ``line_paused`` 而那条待办的语义已经不再是"等人决定"。上限与暂停是一个决定的两半。
        """
        return await self.set_control(
            root,
            tenant_id,
            cap_tokens=int(cap_tokens),
            paused=False,
            reason="",
            updated_by=updated_by,
        )

    async def settle(self, root: str, handle: str, actual_tokens: int) -> None:
        """收尾：摘掉自己的预留，并把**实际**用量并进镜像。

        调用时机必须在 ``finish_run`` 之后 —— 那一行落库才是权威；顺序颠倒时，中间的
        一次回填会把"还没落库的这一次"当成 0，随后的 INCRBY 再补上，结果一致；
        反过来先 INCRBY 再落库，回填可能把同一段算两次（**多算**，对上限是安全的一侧）。
        """
        if not _usable_root(root):
            return
        self._held.pop(handle, None)
        redis = await self._redis_safe()
        if redis is None:
            return
        now = int(time.time())
        try:
            if handle:
                await redis.eval(
                    _RSVD_LUA,
                    1,
                    _rsv_key(root),
                    str(now),
                    "",
                    str(now + RESERVE_TTL_SECONDS),
                    handle,
                    "",
                    str(RESERVE_TTL_SECONDS),
                )
            if int(actual_tokens or 0) > 0:
                await redis.eval(
                    _USED_BUMP_LUA, 1, _used_key(root), str(int(actual_tokens)), str(USED_TTL_SECONDS)
                )
        except Exception as exc:  # noqa: BLE001
            self._warn("settle", "subagent 线账收尾失败（此后不再重复告警，预留会自然到期）: %s", str(exc)[:160])

    async def drain(self, holder: str) -> int:
        """优雅退出：把 ``holder`` 这个实例还挂着的预留全部摘掉，返回摘掉的条数。

        只覆盖"来得及告别"的那一半（正常重启、缩容、发布 —— 恰好是最常制造幽灵预留的场景）；
        ``SIGKILL`` / OOM 走不到这里，那一侧由 :meth:`_sweep_dead_holders` 按存活键收口。
        两者合起来才是完整的：告别让账立刻准，清扫让**没**告别的也只会错一个冷却周期。

        先清登记簿再删：删失败（Redis 正好抖）不该让下次 drain 重试同一批，更不该让进程退不出去。
        """
        if not holder:
            return 0
        roots = sorted(set(self._held.values()))
        self._held.clear()
        if not roots:
            # 这个进程从没预留过（没派发过子 Agent 是常态）⇒ 不建连接、不发命令。
            # 关闭段每个实例都会走到这里，为一个空账去 `_redis_safe()` 会凭空起一条 Redis 连接。
            return 0
        redis = await self._redis_safe()
        if redis is None:
            return 0
        removed = 0
        for root in roots:
            if not _usable_root(root):
                continue
            try:
                res = await redis.eval(_RSVD_DROP_HOLDER_LUA, 1, _rsv_key(root), holder)
                removed += int(res or 0)
            except Exception as exc:  # noqa: BLE001 - 收尾失败由预留的自然到期兜底
                SUBAGENT_LINE_SWEEP.labels(outcome="failed").inc()
                self._warn(
                    "drain", "subagent 线账 drain 失败（此后不再重复告警，预留会自然到期）: %s",
                    str(exc)[:160],
                )
        if removed:
            SUBAGENT_LINE_SWEEP.labels(outcome="drained").inc(removed)
            logger.info(
                "subagent line ledger: released %d reservation(s) for instance %s on shutdown",
                removed, holder,
            )
        return removed


def _used_key(root: str) -> str:
    return rkey(f"subagent:line:{root}:used")


def _rsv_key(root: str) -> str:
    return rkey(f"subagent:line:{root}:rsv")


_ledger: LineLedger | None = None


def get_line_ledger() -> LineLedger:
    """进程内单例（只持有连接，不持有 run 状态）。"""
    global _ledger
    if _ledger is None:
        _ledger = LineLedger()
    return _ledger


async def release_instance_reservations() -> int:
    """lifespan 关闭路径调这个：释放**本实例**的在途预留（M17 的 drain 半边）。

    放在这里而不是 ``app.subagent.registry.stop()`` 里，是因为这条是**唯一保证执行**的关闭路径
    （``_shutdown_lifespan``），而注册表的 stop() 挂在队列 worker 的 ``finally`` 上、worker 没起
    时整个不走。永不抛：关机不能因为"预留没还干净"就退不出去。
    """
    from app.subagent.affinity import cached_instance_id

    try:
        return await get_line_ledger().drain(cached_instance_id())
    except Exception as exc:  # noqa: BLE001
        logger.warning("subagent line drain failed: %s", str(exc)[:160])
        return 0
