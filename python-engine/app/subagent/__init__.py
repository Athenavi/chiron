"""子 Agent 子系统（服务端）：运行记录落库、结果脱敏、L0/L1 分层。

模块职责
--------
* :mod:`app.subagent.store` —— ``subagent_runs`` / ``subagent_run_steps`` 写入端
* :mod:`app.subagent.redact` —— 入库前敏感信息扫描与脱敏

设计见 ``docs/subagent-design.md``。Profile（定义层）在 ``app.agent.profile``。
"""
