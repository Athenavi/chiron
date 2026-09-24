"""Slash Commands subsystem.

Usage::

    from app.commands import registry, CommandContext

    # registry is pre-populated with built-in commands on import
    result = await registry.execute("/help")
"""

from app.commands.builtins import register_builtins
from app.commands.registry import CommandContext, CommandDef, CommandRegistry, registry

# Auto-register built-in commands when the package is imported.
register_builtins()

__all__ = [
    "CommandContext",
    "CommandDef",
    "CommandRegistry",
    "registry",
    "register_builtins",
]
