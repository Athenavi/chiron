"""pytest 收集配置。

这里**只**处理"无法被收集"的文件，不裁剪任何可运行的测试逻辑。

被隔离的 6 个文件分两类，原因不同：

**一、编码损坏（4 个）** —— 中文注释在历史上某次 UTF-8 / GBK 转换中损坏：部分字符变成
私有区码位（U+E0xx / U+E7xx），行尾换行也被吞掉，python 无法解析：

- test_guards.py                      三引号未闭合（SyntaxError）
- test_sandbox_isolation.py           非法不可打印字符（U+E195）
- test_security_tenant_isolation.py   缩进损坏（注释与代码挤在同一行）
- test_trace_writer.py                同上

**二、引用已不存在的符号（1 个）** —— 代码重构后测试没跟上：

- test_engine.py           from app.agent.engine import AgentSession   （该模块没有这个名字）

需要**按当前代码行为重写断言**：原文不可逆（损坏的中文无法还原），或在确认那些能力
是被移除还是被改名之后再决定。在此之前明确隔离，而不是让它们静默失败 ——
让其余测试文件能跑起来，比整个套件在收集阶段就瘫痪要好。

> `test_memory_profile.py` 原先也在此列（它 import 的 `recency_decay` 当时不存在）。
> 该函数与 `rerank_score` 已在 `app/memory/layers.py` 补齐，记忆四层架构的对外接口
> 也已按该文件的断言对齐，因此它已回到常规收集范围。
"""

collect_ignore = [
    # 一、编码损坏
    "test_guards.py",
    "test_sandbox_isolation.py",
    "test_security_tenant_isolation.py",
    "test_trace_writer.py",
    # 二、过时测试（引用已移除 / 改名的符号）
    "test_engine.py",
]


# ── 测试客户端默认携带"网关注入的身份头" ──
#
# 引擎的 AuthMiddleware 现在真的挂上了。此前它只存在于 `_setup_middleware()` ——
# 一条**从未被调用**的旧路径，于是引擎 HTTP 面完全没有认证（详见 app/main.py 的说明）。
#
# 任何带 `?user_id=&tenant_id=` 的请求都必须带 `X-Internal-Token`，而真实链路上
# 这个头是 Go 网关自动注入的（internal/engine/python_client.go:289），并且网关会
# 剥离客户端伪造的同名头（:786）。走 ASGITransport 的测试应当模拟网关的这一行为，
# 否则它验证的就不是真实调用方式 —— 这类"测试自造调用姿势"正是认证缺口长期
# 没被发现的原因之一。
#
# 需要覆盖该头的测试（例如断言 401）在请求上显式传 `headers=` 即可。
import os as _os

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _inject_gateway_internal_token(monkeypatch):
    """让走 ASGITransport 的请求带上网关注入的 `X-Internal-Token`。

    真实链路上这个头由 Go 网关自动注入（internal/engine/python_client.go:289），
    并会剥离客户端伪造的同名头（:786）。测试模拟它是必要的 —— 否则验证的不是
    真实调用方式，而"测试自造调用姿势"正是认证缺口长期没被发现的原因之一。

    注意这里**只注入 header、不注入 query 身份**：带 `?user_id=&tenant_id=` 的
    请求应由测试自己显式构造（那才是"代表某个用户"的语义），统一注入会把身份
    变成写死的 test-user，反而让依赖真实身份的测试失真。
    """
    import httpx

    token = _os.getenv("INTERNAL_TOKEN", "")
    original_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("X-Internal-Token", token)
        original_init(self, *args, headers=headers, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


# 网关路径 = `?user_id=&tenant_id=` 加上 `X-Internal-Token`，两者缺一不可。
# 构造"以某个用户身份经由网关发起"的请求时用这个常量。
GATEWAY_IDENTITY_PARAMS = {"user_id": "test-user", "tenant_id": "test-tenant"}
