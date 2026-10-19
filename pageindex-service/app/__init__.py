"""pageindex-service —— PageIndex 的独立运行环境（检索服务 + 索引 worker）。

与 ``python-engine`` 的关系：**只共享数据库 schema 与 Redis 队列协议**，不共享代码 ——
``pageindex → litellm → openai<3`` 与引擎固定的 ``openai 3.x`` 无法在同一个环境里共存。

* ``config``    —— 环境变量（全部显式）
* ``runtime``   —— Redis 键 / 原文读取 / Cloud key 解密（与引擎同规则，各有契约测试）
* ``adapter``   —— PageIndex 调用面（唯一的 SDK 出口）
* ``search_api``—— 无状态检索服务（HTTP）
* ``index_worker`` —— 消费 ``engine:tasks:kb``，索引并回写状态
* ``store``     —— worker 的数据库访问
"""
