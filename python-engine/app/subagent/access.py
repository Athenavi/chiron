"""子 agent run 的**访问谓词**（一处定义，多处复用）。

`read_subagent_result` / `rerun_subagent` / `resume_subagent` 都要回答同一个问题：
"**这个 run 我能碰吗**"。三份拷贝必然漂移，而漂移方向通常是**放宽**（`vendor/规划.md` §1.3
的同族教训：同一件事写两遍，改动其一必须同时改另一个，而人总会漏一处）。

因此把谓词抽成 SQL 片段常量，由各调用方拼进自己的 SELECT —— 列名与占位符编号（`$2`/`$3`/`$4`）
是它的一部分契约：调用方必须把 `run_id, tenant_id, user_id, root_session_id` 按这个顺序传参。

三条判据（缺一不可）：

* `tenant_id` —— 跨租户一律不可见；
* `user_id = $3 OR user_id IS NULL` —— 同租户内按用户隔离，历史行（`user_id` 为空）保持可见；
* `root_session_id` —— **按对话会话隔离**：主 Agent 不该读到/续跑**别的会话**的子 agent，
  否则它会拿别人的 run 当作自己的上下文。
"""

from __future__ import annotations

#: 拼进 `WHERE` 之后的访问谓词（`$2` = tenant_id · `$3` = user_id · `$4` = root_session_id）。
#: ⚠ 改动这里会同时影响三条读/写路径 —— 已有用例直接断言其中两个子串
#: （`tests/test_subagent_rerun.py` 的 `root_session_id = $4` / `tenant_id = $2`）。
RUN_ACCESS_CLAUSE = """
   AND tenant_id = $2
   AND (user_id = $3 OR user_id IS NULL)
   AND root_session_id = $4
"""
