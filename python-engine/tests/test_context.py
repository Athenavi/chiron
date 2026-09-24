# Context management and CLAUDE.md loader tests
import tempfile
from pathlib import Path

import pytest

from app.context.claude_md import ClaudeMdLoader
from app.context.manager import ContextManager

# ======================================================================
# ContextManager tests
# ======================================================================


class TestCountTokens:
    """1. test_count_tokens — verify token counting."""

    def test_empty_string(self):
        assert ContextManager.count_tokens("") == 0

    def test_whitespace_only(self):
        assert ContextManager.count_tokens("   ") == 0

    def test_short_ascii(self):
        # "hello" = 5 chars -> ceil(5/4) = 2 tokens
        tokens = ContextManager.count_tokens("hello")
        assert tokens == 2

    def test_longer_ascii(self):
        # 40 ASCII chars -> 40/4 = 10 tokens
        text = "a" * 40
        assert ContextManager.count_tokens(text) == 10

    def test_chinese_text(self):
        # Chinese: each char is non-ASCII -> ceil(N/2) tokens
        text = "你好世界"  # 4 chars -> ceil(4/2) = 2 tokens
        assert ContextManager.count_tokens(text) == 2

    def test_mixed_text(self):
        # "hello你好" = 5 ASCII + 2 non-ASCII -> ceil(5/4)+ceil(2/2) = 2+1 = 3
        tokens = ContextManager.count_tokens("hello你好")
        assert tokens == 3

    def test_minimum_one_token(self):
        assert ContextManager.count_tokens("x") == 1


class TestCountMessageTokens:
    """Supplementary: count_message_tokens exercises message-level counting."""

    def test_empty_messages(self):
        cm = ContextManager()
        assert cm.count_message_tokens([]) == 0

    def test_single_message(self):
        cm = ContextManager()
        msgs = [{"role": "user", "content": "hello"}]
        # 4 (overhead) + 2 (tokens for "hello") = 6
        assert cm.count_message_tokens(msgs) == 6

    def test_multiple_messages(self):
        cm = ContextManager()
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        total = cm.count_message_tokens(msgs)
        assert total == 13


class TestCompressMessages:
    """2. test_compress_messages — verify compression reduces token count."""

    @pytest.mark.asyncio
    async def test_no_compression_below_threshold(self):
        cm = ContextManager(max_tokens=1000, compression_threshold=0.8)
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = await cm.compress(messages)
        assert result is messages  # Should return the same object

    @pytest.mark.asyncio
    async def test_compression_reduces_tokens(self):
        # 预算要够放下「system + 摘要 + 尾部」：给得过小会走到 trim_to_fit，
        # 硬截断连摘要一起裁掉（那是降级路径，不是本用例要验的行为）。
        cm = ContextManager(max_tokens=4000, compression_threshold=0.8)

        # Build a message list with substantial content that exceeds threshold
        messages = [{"role": "system", "content": "System prompt here."}]
        filler_u = "This is a user message with enough content to accumulate tokens quickly. "
        filler_a = "This is an assistant reply with enough content to accumulate tokens quickly. "
        for i in range(30):
            messages.append({"role": "user", "content": f"Message {i}: " + filler_u * 3})
            messages.append({"role": "assistant", "content": f"Reply {i}: " + filler_a * 3})

        original_tokens = cm.count_message_tokens(messages)
        assert original_tokens > cm.max_tokens * cm.compression_threshold

        result = await cm.compress(messages)
        new_tokens = cm.count_message_tokens(result)

        assert new_tokens < original_tokens
        # System prompt should still be first
        assert result[0]["role"] == "system"
        # A context summary message should be present
        assert any("[Context Summary]" in m.get("content", "") for m in result)

    @pytest.mark.asyncio
    async def test_compression_preserves_tail(self):
        cm = ContextManager(max_tokens=80, compression_threshold=0.8)

        messages = [{"role": "system", "content": "You are helpful."}]
        for i in range(15):
            messages.append({"role": "user", "content": f"Question {i}"})
            messages.append({"role": "assistant", "content": f"Answer {i}"})

        result = await cm.compress(messages)

        # The last message in the original should still be the last in the result
        assert result[-1]["content"] == messages[-1]["content"]


class TestTrimToFit:
    """3. test_trim_to_fit — verify trimming removes oldest messages."""

    def test_no_trimming_needed(self):
        cm = ContextManager(max_tokens=100_000)
        messages = [
            {"role": "system", "content": "Sys"},
            {"role": "user", "content": "Hi"},
        ]
        result = cm.trim_to_fit(messages)
        assert len(result) == len(messages)

    def test_trimming_removes_oldest(self):
        cm = ContextManager(max_tokens=100_000)

        messages = [{"role": "system", "content": "System prompt."}]
        for i in range(100):
            messages.append({"role": "user", "content": f"Message {i} " + "x" * 200})
            messages.append({"role": "assistant", "content": f"Response {i} " + "y" * 200})

        # Set a very tight budget
        result = cm.trim_to_fit(messages, max_tokens=500)

        # Should have fewer messages
        assert len(result) < len(messages)
        # System message must survive
        assert result[0]["role"] == "system"
        # Latest messages should survive (the tail)
        assert result[-1]["content"] == messages[-1]["content"]

    def test_custom_max_tokens(self):
        cm = ContextManager(max_tokens=200_000)
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "a" * 400},
            {"role": "assistant", "content": "b" * 400},
            {"role": "user", "content": "c" * 400},
            {"role": "assistant", "content": "d" * 400},
        ]
        # With a budget of 200 tokens, most messages should be trimmed
        result = cm.trim_to_fit(messages, max_tokens=200)
        assert cm.count_message_tokens(result) <= 200
        assert result[0]["role"] == "system"


# ======================================================================
# ClaudeMdLoader tests
# ======================================================================


class TestClaudeMdLoader:
    """4. test_load_claude_md — create temp CLAUDE.md, verify loading."""

    def test_load_from_project_root(self):
        """CLAUDE.md in the given root directory should be found and loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            content = "# Project Rules\nUse Python 3.11+"
            (Path(tmpdir) / "CLAUDE.md").write_text(content, encoding="utf-8")

            loader = ClaudeMdLoader()
            result = loader.load(tmpdir)

            assert "Project Rules" in result
            assert "Python 3.11+" in result

    def test_hierarchical_loading(self):
        """CLAUDE.md files at multiple levels should all be merged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sub = Path(tmpdir) / "sub"
            sub2 = sub / "sub2"
            sub2.mkdir(parents=True)

            (Path(tmpdir) / "CLAUDE.md").write_text("Root rules", encoding="utf-8")
            (sub / "CLAUDE.md").write_text("Sub rules", encoding="utf-8")
            (sub2 / "CLAUDE.md").write_text("Sub2 rules", encoding="utf-8")

            loader = ClaudeMdLoader()
            result = loader.load(str(sub2))

            # All three files should appear
            assert "Root rules" in result
            assert "Sub rules" in result
            assert "Sub2 rules" in result

    def test_no_claude_md_returns_empty(self, monkeypatch):
        """When no CLAUDE.md exists, return empty string."""
        # Patch _HOME_DIR so a global ~/.claude/CLAUDE.md doesn't interfere
        import app.context.claude_md as mod
        monkeypatch.setattr(mod, "_HOME_DIR", Path("/__nonexistent__/CLAUDE.md"))

        with tempfile.TemporaryDirectory() as tmpdir:
            loader = ClaudeMdLoader()
            result = loader.load(tmpdir)
            assert result == ""

    def test_cache_works(self):
        """Second load should hit cache and return same result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "CLAUDE.md").write_text("Cached content", encoding="utf-8")

            loader = ClaudeMdLoader()
            result1 = loader.load(tmpdir)
            result2 = loader.load(tmpdir)
            assert result1 == result2

    def test_cache_invalidation(self):
        """After clear_cache, load should re-read from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_path = Path(tmpdir) / "CLAUDE.md"
            claude_path.write_text("Version 1", encoding="utf-8")

            loader = ClaudeMdLoader()
            result1 = loader.load(tmpdir)
            assert "Version 1" in result1

            # Modify the file
            claude_path.write_text("Version 2", encoding="utf-8")
            loader.clear_cache()

            result2 = loader.load(tmpdir)
            assert "Version 2" in result2
            assert "Version 1" not in result2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
