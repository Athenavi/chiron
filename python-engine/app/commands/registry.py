"""Slash Commands registry.

Provides a unified command registration / lookup / execution interface.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CommandDef:
    """Definition of a single slash command."""

    name: str
    description: str
    handler: Callable[[str, Any], Awaitable[str]]


@dataclass
class CommandContext:
    """Mutable context bag passed to every command handler.

    Attributes can be set by the host application (conversation history,
    current model, memory manager, etc.).
    """

    history: list[dict[str, str]] = field(default_factory=list)
    model: str = ""
    temperature: float = 0.7
    system_prompt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class CommandRegistry:
    """Process-wide slash-command registry."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandDef] = {}

    # ── registration ────────────────────────────────────────────

    def register(
        self,
        name: str,
        description: str,
        handler: Callable[[str, Any], Awaitable[str]],
    ) -> None:
        """Register (or overwrite) a command."""
        self._commands[name] = CommandDef(
            name=name,
            description=description,
            handler=handler,
        )

    # ── lookup ──────────────────────────────────────────────────

    def get(self, name: str) -> CommandDef | None:
        return self._commands.get(name)

    def list_commands(self) -> list[CommandDef]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    # ── parsing ─────────────────────────────────────────────────

    @staticmethod
    def parse(raw: str) -> tuple[str, str]:
        """Parse a raw string like '/model gpt-4' into ('model', 'gpt-4').

        Also handles bare '/help' -> ('help', '').
        """
        stripped = raw.strip()
        if stripped.startswith("/"):
            stripped = stripped[1:]
        parts = stripped.split(None, 1)
        name = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        return name, args

    # ── execution ───────────────────────────────────────────────

    async def execute(
        self,
        raw: str,
        context: CommandContext | None = None,
    ) -> str:
        """Parse *raw* input, look up the command, and run it.

        Returns the handler's string result, or an error message when the
        command is not found.
        """
        name, args = self.parse(raw)
        cmd = self._commands.get(name)
        if cmd is None:
            return f"Unknown command: /{name}. Type /help for available commands."
        return await cmd.handler(args, context)


# ── process-wide singleton ─────────────────────────────────────
registry = CommandRegistry()
