"""真实存储上的集成测试包（``tests/integration``）。

与 ``tests/`` 下的单元测试分开：那份把 PG / Redis 全关、把 ``asyncpg`` 换成替身，
只验证 HTTP 与纯逻辑的形状；这份用**真实 PostgreSQL**（``PostgresServer`` + 真实
``alembic upgrade head``）+ 真实 HTTP 打完整链路。

起不来就 ``skip``（见 ``conftest.pg_server`` 的逐条错误），不伪造通过。
"""
