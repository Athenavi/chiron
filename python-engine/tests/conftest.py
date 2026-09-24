"""pytest 收集配置。

历史上这里用 ``collect_ignore`` 隔离过 5 个"无法被收集"的文件；它们现已**全部修复**，
隔离清单因此清空（保留本说明以免后人重复踩坑）：

**一、编码损坏（4 个，已修复）** —— 中文注释在历史上某次 UTF-8/GBK 混转中损坏
（部分字符变成私用区码位、行尾换行与个别引号被吞）。经确认这类乱码**可逆向还原**
（``line.encode('gbk').decode('utf-8')``），据此按原文修复，未改动断言逻辑：

- test_guards.py                      三引号未闭合
- test_sandbox_isolation.py           非法不可打印字符（U+E195）
- test_security_tenant_isolation.py   缩进损坏（注释与代码挤在同一行）
- test_trace_writer.py                同上

**二、引用已移除的符号（1 个，已清理）** —— test_engine.py 曾 import ``AgentSession`` /
``ToolApprovalRequest`` / ``ToolApprovalResponse``；这些符号随旧版引擎重构一并移除
（全仓无定义）。测这些已删除 API 的类整体移除，其余保留。原覆盖能力见
tests/test_runtime.py、tests/test_context.py、tests/test_prompt_engine.py。

> 教训：这 5 个文件合计约 52KB 的测试代码**从未执行**，其中包含 sandbox 逃逸、租户隔离
> 等安全用例。``collect_ignore`` 让其余测试能跑起来，但也掩盖了它们长期失效 ——
> 今后新增隔离项时，请同时写清"何时、按什么条件解除"。

> 附记：``test_memory_profile.py`` 原先也在此列（它 import 的 ``recency_decay`` 当时不存在）。
> 该函数与 ``rerank_score`` 已在 ``app/memory/layers.py`` 补齐，因此它已回到常规收集范围。
"""


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
import pytest as _pytest  # noqa: E402 — 紧接上方的 fixture 说明


@_pytest.fixture(autouse=True)
def _inject_gateway_internal_token(monkeypatch):
    """让走 ASGITransport 的请求带上网关注入的 `X-Internal-Token`。

    真实链路上这个头由 Go 网关自动注入（internal/engine/python_client.go:289），
    并会剥离客户端伪造的同名头（:786）。测试模拟它是必要的 —— 否则验证的不是
    真实调用方式，而"测试自造调用姿势"正是认证缺口长期没被发现的原因之一。

    注意这里**只注入 header、不注入 query 身份**：带 `?user_id=&tenant_id=` 的
    请求应由测试自己显式构造（那才是"代表某个用户"的语义），统一注入会把身份
    变成写死的 test-user，反而让依赖真实身份的测试失真。

    取值的来源必须是**引擎自己实际使用的那个 token**（`settings.internal_token`），
    而不是 `os.getenv("INTERNAL_TOKEN")`：pytest 进程的环境里通常没有这个变量
    （`.env` 由 pydantic 读进 settings，不改 `os.environ`），拿 env 只会注入一个
    空头 —— 中间件 fail-close，于是所有走 ASGITransport 的请求都 401。
    这批"测试自己带错 token"造成的红，正是认证中间件长期没被发现的原因之一。
    """
    import httpx

    from app.config import settings

    token = settings.internal_token
    original_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("X-Internal-Token", token)
        original_init(self, *args, headers=headers, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


# 网关路径 = `?user_id=&tenant_id=` 加上 `X-Internal-Token`，两者缺一不可。
# 构造"以某个用户身份经由网关发起"的请求时用这个常量。
GATEWAY_IDENTITY_PARAMS = {"user_id": "test-user", "tenant_id": "test-tenant"}
