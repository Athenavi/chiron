"""给 `tests/source_scan.py` 上锁：剥注释必须**只**去掉注释，不能动代码文本。"""

from __future__ import annotations

from tests.source_scan import without_comments


def test_comment_only_line_is_gone():
    assert "start_periodic_cleanup" not in without_comments(
        "# await store.start_periodic_cleanup(on_expired=x)\n"
    )


def test_trailing_comment_is_removed_but_code_kept():
    out = without_comments("await store.start_periodic_cleanup(on_expired=x)  # 注释\n")
    assert "start_periodic_cleanup(on_expired=x)" in out
    assert "注释" not in out


def test_code_text_is_preserved_character_for_character():
    """关键：不能 join token（那会插入空格，破坏 `fn(arg=` 这类子串）。"""
    src = 'if evt.type == "thinking" and evt.content:\n    pass\n'
    assert without_comments(src) == src


def test_multiple_comments_on_one_line():
    out = without_comments("x = 1  # a\n# b\ny = 2  # c\n")
    assert "a" not in out and "b" not in out and "c" not in out
    assert "x = 1" in out and "y = 2" in out


def test_hash_inside_string_is_not_a_comment():
    """字符串里的 `#` 不是注释（tokenize 分得清，正则分不清）。"""
    src = 'x = "a # b"\n'
    assert without_comments(src) == src
