"""Tests for the PromptEngine — system prompt assembly."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.prompt_engine import PromptEngine
from app.agent.runtime import AgentTask

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(**overrides) -> AgentTask:
    """Create a minimal AgentTask with sensible defaults."""
    defaults = dict(
        id="task-1",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        content="Fix the bug in auth.py",
        system_prompt="",
        history=[],
        tools=[],
        llm_config={},
        max_turns=10,
    )
    defaults.update(overrides)
    return AgentTask(**defaults)


# ---------------------------------------------------------------------------
# 1. test_assemble_basic — verify base prompt is included
# ---------------------------------------------------------------------------

class TestAssembleBasic:

    @pytest.mark.asyncio
    async def test_default_template_used_when_no_system_prompt(self):
        """When the task has no system_prompt the default template is used."""
        engine = PromptEngine()
        task = _make_task(system_prompt="")
        result = await engine.assemble(task, tools=[])

        # The default template header should appear.
        assert "System Prompt" in result
        assert "AI coding assistant" in result

    @pytest.mark.asyncio
    async def test_task_system_prompt_used_as_base(self):
        """When the task carries a system_prompt it becomes the base."""
        engine = PromptEngine()
        task = _make_task(system_prompt="You are a Python expert.")
        result = await engine.assemble(task, tools=[])

        assert result.startswith("You are a Python expert.")

    @pytest.mark.asyncio
    async def test_task_system_prompt_stripped(self):
        """Whitespace-only system_prompt falls back to the default template."""
        engine = PromptEngine()
        task = _make_task(system_prompt="   \n  ")
        result = await engine.assemble(task, tools=[])

        assert "System Prompt" in result


# ---------------------------------------------------------------------------
# 2. test_load_claude_md — create temp CLAUDE.md, verify it's loaded
# ---------------------------------------------------------------------------

class TestLoadClaudeMd:

    def test_load_existing_file(self):
        """CLAUDE.md content is returned when the file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_path = Path(tmpdir) / "CLAUDE.md"
            claude_path.write_text("# Project Rules\nUse black for formatting.", encoding="utf-8")

            engine = PromptEngine()
            content = engine._load_claude_md(tmpdir)

            assert "Project Rules" in content
            assert "black" in content

    def test_load_case_insensitive(self):
        """Also picks up lowercase 'claude.md'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "claude.md").write_text("lowercase file", encoding="utf-8")

            engine = PromptEngine()
            content = engine._load_claude_md(tmpdir)

            assert "lowercase file" in content

    def test_missing_file_returns_empty(self):
        """Returns empty string when no CLAUDE.md exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = PromptEngine()
            content = engine._load_claude_md(tmpdir)
            assert content == ""

    @pytest.mark.asyncio
    async def test_assemble_includes_claude_md(self):
        """assemble() includes CLAUDE.md content in the final prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "CLAUDE.md").write_text("Always use type hints.", encoding="utf-8")

            # Point _resolve_root to our temp dir
            old_root = os.environ.get("PROMPT_ENGINE_ROOT")
            os.environ["PROMPT_ENGINE_ROOT"] = tmpdir
            try:
                engine = PromptEngine()
                task = _make_task(system_prompt="You are a coder.")
                result = await engine.assemble(task, tools=[])
                assert "type hints" in result
            finally:
                if old_root is None:
                    os.environ.pop("PROMPT_ENGINE_ROOT", None)
                else:
                    os.environ["PROMPT_ENGINE_ROOT"] = old_root


# ---------------------------------------------------------------------------
# 3. test_get_git_context — verify git info is included
# ---------------------------------------------------------------------------

class TestGetGitContext:

    def test_git_context_in_repo(self):
        """Git context includes branch and recent commits when in a repo."""
        engine = PromptEngine()
        # Use the actual project root which is guaranteed to be a git repo.
        project_root = str(Path(__file__).resolve().parents[2])
        ctx = engine._get_git_context(project_root)

        if ctx:  # git may not be installed in CI
            assert "Branch" in ctx

    def test_git_context_non_repo(self):
        """Returns empty string in a non-git directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = PromptEngine()
            ctx = engine._get_git_context(tmpdir)
            assert ctx == ""

    @pytest.mark.asyncio
    async def test_assemble_includes_git_context(self, monkeypatch):
        """assemble() includes git context when available."""
        # Mock _get_git_context to always return a known string
        engine = PromptEngine()
        monkeypatch.setattr(engine, "_get_git_context", lambda root: "**Branch:** main\n**Recent commits:**\n```\nabc1234 fix stuff\n```")

        task = _make_task(system_prompt="You are a coder.")
        result = await engine.assemble(task, tools=[])

        assert "main" in result
        assert "fix stuff" in result


# ---------------------------------------------------------------------------
# 4. test_format_tools — verify tools are formatted correctly
# ---------------------------------------------------------------------------

class TestFormatTools:

    def test_basic_format(self):
        """Tool dicts are formatted into a readable list."""
        engine = PromptEngine()
        tools = [
            {"name": "read_file", "description": "Read a file from disk."},
            {"name": "shell_exec", "description": "Execute a shell command."},
        ]
        result = engine._format_tools(tools)

        assert "**read_file**" in result
        assert "Read a file from disk." in result
        assert "**shell_exec**" in result

    def test_openai_format(self):
        """OpenAI function-calling format is supported."""
        engine = PromptEngine()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "grep_files",
                    "description": "Search files for a pattern.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        result = engine._format_tools(tools)

        assert "**grep_files**" in result
        assert "Search files for a pattern." in result

    def test_empty_tools(self):
        """Empty tool list returns empty string."""
        engine = PromptEngine()
        assert engine._format_tools([]) == ""

    def test_long_description_truncated(self):
        """Descriptions longer than 300 chars are truncated."""
        engine = PromptEngine()
        long_desc = "A" * 400
        tools = [{"name": "big_tool", "description": long_desc}]
        result = engine._format_tools(tools)

        assert "…" in result
        assert len(result) < 450  # reasonable upper bound

    def test_missing_description(self):
        """Tools without a description still appear."""
        engine = PromptEngine()
        tools = [{"name": "mystery"}]
        result = engine._format_tools(tools)

        assert "**mystery**" in result


# ---------------------------------------------------------------------------
# Bonus: memory & skills integration
# ---------------------------------------------------------------------------

class TestMemoryAndSkillsIntegration:

    @pytest.mark.asyncio
    async def test_memory_context_with_mock_manager(self):
        """Memory context is injected when a MemoryManager is available."""
        mock_manager = AsyncMock()
        mock_manager.query_memory.return_value = [
            {"content": "User prefers Python", "relevance": 0.95, "memory_type": "long_term"},
            {"content": "Last task was about auth", "relevance": 0.80, "memory_type": "short_term"},
        ]

        engine = PromptEngine(memory_manager=mock_manager)
        task = _make_task(system_prompt="You are helpful.", user_id="user-1")
        result = await engine.assemble(task, tools=[])

        assert "User prefers Python" in result
        assert "Last task was about auth" in result

    @pytest.mark.asyncio
    async def test_skills_context_with_mock_store(self):
        """Skills context is injected when a SkillStore is available."""
        from app.skill.store import SkillDef

        mock_store = MagicMock()
        mock_store.list.return_value = [
            SkillDef(name="prd_generate", description="Generate a PRD", tags=["pm"]),
        ]

        engine = PromptEngine(skill_store=mock_store)
        task = _make_task(system_prompt="You are helpful.")
        result = await engine.assemble(task, tools=[])

        assert "prd_generate" in result
        assert "Generate a PRD" in result

    @pytest.mark.asyncio
    async def test_no_managers_no_crash(self):
        """Engine works fine when no optional managers are injected."""
        engine = PromptEngine()
        task = _make_task(system_prompt="You are helpful.")
        result = await engine.assemble(task, tools=[])

        assert "You are helpful." in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
