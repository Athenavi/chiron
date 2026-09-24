"""Tests for the slash commands system."""
import pytest

from app.commands import CommandContext, CommandRegistry, registry

# ── helpers ─────────────────────────────────────────────────────

@pytest.fixture
def fresh_registry():
    """Return a *clean* CommandRegistry so built-ins don't leak between tests."""
    return CommandRegistry()


@pytest.fixture
def ctx():
    return CommandContext(
        history=[{"role": "user", "content": "hello"}],
        model="test-model",
        temperature=0.5,
    )


# ── test_register_and_execute ───────────────────────────────────

@pytest.mark.asyncio
async def test_register_and_execute(fresh_registry):
    """Register a custom command and execute it."""
    async def ping(args: str, ctx) -> str:
        return f"pong: {args}"

    fresh_registry.register("ping", "Ping the server", ping)

    result = await fresh_registry.execute("/ping hello world")
    assert result == "pong: hello world"


# ── test_help_command ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_help_command():
    """/help should list all registered built-in commands."""
    # Use the global registry (built-ins auto-registered on import)
    result = await registry.execute("/help")
    # Spot-check that every known built-in appears
    for cmd in ("help", "clear", "compact", "model", "temperature",
                "think", "act", "skill", "memory", "context",
                "cost", "undo", "retry"):
        assert f"/{cmd}" in result, f"/{cmd} missing from /help output"


# ── test_unknown_command ────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_command(fresh_registry):
    """Executing an unknown command should return an error message."""
    result = await fresh_registry.execute("/doesnotexist")
    assert "Unknown command" in result
    assert "/doesnotexist" in result


# ── test_clear_command ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_command(ctx):
    """/clear should empty the conversation history and return confirmation."""
    assert len(ctx.history) == 1  # pre-condition

    result = await registry.execute("/clear", ctx)
    assert result == "Conversation history cleared."
    assert len(ctx.history) == 0


# ── extra coverage ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_parse():
    """CommandRegistry.parse splits name and args correctly."""
    assert CommandRegistry.parse("/model gpt-4") == ("model", "gpt-4")
    assert CommandRegistry.parse("/help") == ("help", "")
    assert CommandRegistry.parse("model gpt-4") == ("model", "gpt-4")


@pytest.mark.asyncio
async def test_model_switch(ctx):
    """/model should update the context model."""
    result = await registry.execute("/model gpt-4o", ctx)
    assert "gpt-4o" in result
    assert ctx.model == "gpt-4o"


@pytest.mark.asyncio
async def test_temperature_set(ctx):
    """/temperature should validate and set the value."""
    result = await registry.execute("/temperature 1.2", ctx)
    assert "1.2" in result
    assert ctx.temperature == 1.2


@pytest.mark.asyncio
async def test_temperature_invalid():
    result = await registry.execute("/temperature abc")
    assert "Invalid" in result


@pytest.mark.asyncio
async def test_think_and_act(ctx):
    """/think and /act should toggle the mode metadata."""
    await registry.execute("/think", ctx)
    assert ctx.metadata["mode"] == "think"

    await registry.execute("/act", ctx)
    assert ctx.metadata["mode"] == "act"


@pytest.mark.asyncio
async def test_context_command(ctx):
    """/context should report current settings."""
    result = await registry.execute("/context", ctx)
    assert "test-model" in result


@pytest.mark.asyncio
async def test_compact(ctx):
    """/compact should summarise and clear history."""
    result = await registry.execute("/compact", ctx)
    assert "Compacted" in result
    assert len(ctx.history) == 1  # replaced by summary
    assert "Compacted" in ctx.history[0]["content"]
