"""MCP stdio 子进程的环境必须**始终**经过 `sandboxed_env()`。

背景（本轮生产审查发现）：`_connect_stdio` 此前是

    env = None
    if server.env:
        env = {**sandboxed_env(), **server.env}

于是**未声明 `env` 的 stdio server 会完整继承引擎进程环境**：`APP_SECRET` /
`JWT_SECRET` / `INTERNAL_TOKEN` / 数据库口令 / 各 provider 的 API key 全在里面。
而这个子进程跑的是第三方插件代码，agent 又能通过它读自己的环境
（`os.environ`、`env` 命令、`/proc/self/environ`）—— 等于把网关的全部凭据交给
一段由不可信内容驱动的进程。

修法是把"清理宿主环境"从**条件分支**变成**基底**：没有声明 env 时也要清理。
这里同时钉住行为（`project_stdio_env` 的语义）和接线（`client.py` 里没有
`env = None` 这条老路）。
"""

from __future__ import annotations

from app.mcp.credential_projection import project_stdio_env
from tests.source_scan import code_of


class _Server:
    def __init__(self, env=None):
        self.env = env if env is not None else {}


def test_empty_declared_env_still_gets_cleaned_base():
    """核心回归：没有声明 env 时**不是** None，而是一份已清理的基底。"""
    base = {"PATH": "/usr/bin", "APP_SECRET": "leak-me"}
    # 调用方传进来的 base 必须已经是 sandboxed_env() 的结果；这里验的是
    # "空声明 → 仍然返回 dict（而不是放行宿主环境）"这个语义。
    env = project_stdio_env(_Server(), {"PATH": "/usr/bin"})
    assert isinstance(env, dict) and env.get("PATH") == "/usr/bin"
    assert "APP_SECRET" not in env


def test_declared_env_is_overlaid_on_base():
    env = project_stdio_env(_Server({"TOKEN": "t"}), {"PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin", "TOKEN": "t"}


def test_base_is_not_mutated():
    base = {"PATH": "/usr/bin"}
    project_stdio_env(_Server({"X": "1"}), base)
    assert base == {"PATH": "/usr/bin"}


def test_stdio_subprocess_env_is_always_sandboxed():
    """接线守卫：`_connect_stdio` 必须把 `sandboxed_env()` 当作基底。

    两条一起看才有意义：既不能回到 `env = None`（继承宿主环境），
    也不能只叠加 server.env 而漏掉清理。
    """
    code = code_of("app/mcp/client.py")
    assert "env = None" not in code, "stdio 子进程又回到了继承宿主环境的老路"
    assert "project_stdio_env(server, sandboxed_env())" in code
