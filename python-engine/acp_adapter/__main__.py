"""`python -m acp_adapter`：ACP 的 stdio 服务端入口。

**stdout 是 JSON-RPC 通道**，所以日志一律走 stderr —— 往 stdout 打一行日志就会污染协议，
而症状是客户端报"解析失败"，看不出是谁干的。
"""

from __future__ import annotations

import logging
import sys


def main() -> int:
    from acp_adapter.agent import ChironACPAgent, require_sdk

    # 缺 SDK / 缺 API Key 都在这里以明确信息退出（不进入交互）
    require_sdk()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    from acp import run_agent  # type: ignore[import-not-found]

    agent = ChironACPAgent()
    # 具体的 serve 形状以 SDK 版本为准（参照实现用的就是 `run_agent`）。
    run_agent(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
