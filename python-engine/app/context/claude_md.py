# CLAUDE.md hierarchical loader
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Well-known locations (highest → lowest priority)
_HOME_DIR = Path.home() / ".claude" / "CLAUDE.md"


class ClaudeMdLoader:
    """Load and merge CLAUDE.md files from the project hierarchy.

    Scans from *root* upward to the filesystem root, then checks
    ``~/.claude/CLAUDE.md``.  All found files are concatenated (highest
    priority first = nearest to *root*), separated by a divider.
    Results are cached so repeated calls are cheap.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, root: str = ".") -> str:
        """Load and merge all CLAUDE.md files found in the hierarchy.

        Args:
            root: Starting directory (default ``"."``).

        Returns:
            Merged content of all CLAUDE.md files, or an empty string if none
            are found.
        """
        resolved = str(Path(root).resolve())
        if resolved in self._cache:
            return self._cache[resolved]

        files = self._find_claude_md(root)

        if not files:
            self._cache[resolved] = ""
            return ""

        parts: list[str] = []
        for filepath in files:
            try:
                text = Path(filepath).read_text(encoding="utf-8")
                if text.strip():
                    header = f"<!-- CLAUDE.md: {filepath} -->"
                    parts.append(f"{header}\n{text.strip()}")
            except OSError as exc:
                logger.warning("Could not read %s: %s", filepath, exc)

        merged = "\n\n---\n\n".join(parts)
        self._cache[resolved] = merged
        return merged

    def clear_cache(self) -> None:
        """Invalidate the internal cache."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _find_claude_md(root: str) -> list[str]:
        """Find all CLAUDE.md files in the hierarchy.

        Walks from *root* to its parents (inclusive), then checks
        ``~/.claude/CLAUDE.md``.  Returns paths ordered from nearest to
        *root* (highest priority) to farthest.

        Args:
            root: Starting directory.

        Returns:
            List of absolute paths to existing CLAUDE.md files.
        """
        results: list[str] = []
        current = Path(root).resolve()

        # Walk upward from root
        while True:
            candidate = current / "CLAUDE.md"
            if candidate.is_file():
                results.append(str(candidate))

            parent = current.parent
            if parent == current:
                # Reached filesystem root
                break
            current = parent

        # Global fallback
        if _HOME_DIR.is_file():
            home_path = str(_HOME_DIR)
            if home_path not in results:
                results.append(home_path)

        return results
