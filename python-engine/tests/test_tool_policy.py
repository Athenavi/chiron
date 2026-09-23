"""工具动作分级与参数归一化（app/agent/tool_policy.py）。

回归保护：
  * 分级取代了 guards.py 里并列的 WRITE_TOOLS / DANGEROUS_TOOLS —— 那两套名单已彼此不一致
    （`browser_screenshot` 只在后者里），用工具名表达策略必然漂移；
  * 未声明工具必须 fail-closed 到 write（漏登记 = 多一次确认，而不是少一次）；
  * 命令类工具按**内容**判定：`shell_exec` 既能 `ls` 也能 `rm -rf`；
  * 参数归一化是票据绑定与重复检测的共同基础 —— 改个写法就能绕开的话，两者都等于没有。
"""
from app.agent.tool_policy import (
    DELETE,
    EXTERNAL,
    READ,
    WRITE,
    args_hash,
    canonical_args,
    requires_confirmation,
    requires_second_check,
    tool_level,
)


def test_levels_for_representative_tools():
    assert tool_level("read_file") == READ
    assert tool_level("grep_files") == READ
    assert tool_level("write_file") == WRITE
    assert tool_level("web_fetch") == EXTERNAL
    assert tool_level("skill_install") == EXTERNAL
    assert tool_level("job_kill") == DELETE


def test_prefix_rule_covers_dynamic_tool_names():
    # 工具名随 action 变化（browser_navigate / browser_click / …）：前缀规则保证不漏
    assert tool_level("browser_click") == EXTERNAL
    assert tool_level("browser_navigate") == EXTERNAL
    assert tool_level("web_something_new") == EXTERNAL


def test_unknown_tool_is_fail_closed():
    assert tool_level("brand_new_tool_xyz") == WRITE
    # 未声明 → ask 下要确认、auto 下不拦（与 write 一致），绝不是 read
    assert requires_confirmation(tool_level("brand_new_tool_xyz"), "ask") is True


def test_command_tool_level_depends_on_content():
    assert tool_level("shell_exec", {"command": "rm -rf /tmp/x"}) == DELETE
    assert tool_level("shell_exec", {"command": "DROP TABLE users"}) == DELETE
    assert tool_level("shell_exec", {"command": "git push --force"}) == DELETE
    assert tool_level("run_code", {"code": "shutil.rmtree('x')"}) == DELETE
    assert tool_level("shell_exec", {"command": "ls -la"}) == WRITE
    assert tool_level("shell_exec", {"command": "git status"}) == WRITE
    # 无法判定（没有命令文本）时保守停在 DELETE
    assert tool_level("shell_exec", {}) == DELETE


def test_confirmation_matrix():
    for mode, expected in (("ask", True), ("auto", False), ("yolo", False)):
        assert requires_confirmation(WRITE, mode) is expected, mode
    for mode, expected in (("ask", True), ("auto", True), ("yolo", False)):
        assert requires_confirmation(DELETE, mode) is expected, mode
        assert requires_confirmation(EXTERNAL, mode) is expected, mode
    assert requires_confirmation(READ, "ask") is False


def test_second_check_only_for_irreversible_and_external():
    assert requires_second_check(DELETE) is True
    assert requires_second_check(EXTERNAL) is True
    assert requires_second_check(WRITE) is False
    assert requires_second_check(READ) is False


def test_args_hash_normalizes_paths_and_ordering():
    # 同一个意图的不同写法必须同哈希
    assert args_hash("read_file", {"path": "./a.txt"}) == args_hash("read_file", {"path": "a.txt"})
    assert args_hash("read_file", {"path": "a/b/"}) == args_hash("read_file", {"path": "a/b"})
    # 键顺序无关 + 字符串去空白
    assert args_hash("write_file", {"path": "a", "content": " x "}) == args_hash(
        "write_file", {"content": "x", "path": "a"}
    )
    # 真正不同 → 不同哈希
    assert args_hash("read_file", {"path": "a"}) != args_hash("read_file", {"path": "b"})
    assert args_hash("read_file", {"path": "a"}) != args_hash("write_file", {"path": "a"})


def test_canonical_args_tolerates_bad_input():
    assert canonical_args("read_file", None) == {}
    assert canonical_args("read_file", "not-a-dict") == {}  # type: ignore[arg-type]
