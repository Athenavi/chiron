"""技能目录的**只读**后端（挂 `/skills/`）—— 让"目录里给路径、按需读正文"真的成立。

## 为什么需要它（D2）

技能目录在 `data/skills/{tenant}/{user}/...`（`app/skill/store.py` 的四级解析），**不在** agent
沙箱工作区里 —— 所以 `read_file` 原本读不到技能正文。这样一来，"目录注入给出可读路径 + 提示用
`read_file` 读正文"就只是**摆设**（模型照着做必然失败）。本后端把该目录挂到虚拟路径 `/skills/`，
复用批 B 的 `CompositeBackend` 机制（不新造通道）。

## 只读是硬约束

技能目录是**部署 / 租户资产**：agent 通过文件工具改写它，等于绕过技能的管理面（安装 → 校验 →
审计那条链）。因此 `write` / `edit` 一律拒绝 —— 这不是"暂未实现"，而是设计选择。

## 身份

`SkillStore()` 的身份来自 tool context（tenant/user），**每次调用按当前身份新建** —— 刻意不做
模块级单例：单例会把身份固定成"第一个调用者"，那是跨租户读技能目录的严重缺陷。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.backends.protocol import (
    EditResult,
    FileStat,
    GlobResult,
    GrepResult,
    LsResult,
    ReadBytesResult,
    ReadResult,
    WriteResult,
)

logger = logging.getLogger(__name__)

#: 虚拟挂载点（目录注入与 `read_file` 都用它）
MOUNT = "/skills/"

_READ_ONLY = "skills are read-only through the file backend; use the skill management API instead"


def _store() -> Any:
    """按**当前身份**构造技能 store（见模块头：刻意不做单例）。"""
    from app.skill.store import SkillStore

    return SkillStore()


def _norm(path: str) -> str:
    text = (path or "").strip()
    if text.startswith(MOUNT):
        text = text[len(MOUNT) :]
    elif text.startswith("skills/"):
        text = text[len("skills/") :]
    return text.strip("/")


class SkillBackend:
    """`/skills/<name>/<relative-path>` → 技能目录内的文件（只读）。"""

    def _resolve(self, path: str) -> tuple[Any, str, str]:
        """返回 `(skill, 目录内相对路径, 错误)`；错误非空时前两项无意义。"""
        rel = _norm(path)
        if not rel:
            return None, "", "path must be /skills/<skill-name>/<file>"
        parts = rel.split("/", 1)
        name = parts[0]
        inner = parts[1] if len(parts) > 1 else ""
        try:
            skill = _store().get(name)
        except Exception as exc:  # noqa: BLE001 — store 故障按"找不到"处理（工具会报明确错误）
            logger.warning("skill backend: store lookup failed for %r: %s", name, exc)
            return None, "", f"skill store unavailable: {str(exc)[:120]}"
        if skill is None:
            return None, "", f"skill not found: {name}"
        source_dir = str(getattr(skill, "source_dir", "") or "")
        if not source_dir:
            return None, "", f"skill {name!r} has no readable directory (flat .skill.json)"
        return skill, inner, ""

    def _file(self, skill: Any, inner: str) -> tuple[Path | None, str]:
        """把目录内相对路径解析成真实文件；越界/缺失都返回错误。"""
        if not inner:
            return None, "path must point to a file inside the skill directory"
        root = Path(str(getattr(skill, "source_dir", ""))).resolve()
        try:
            candidate = (root / inner).resolve()
        except (OSError, ValueError) as exc:
            return None, f"invalid path: {str(exc)[:120]}"
        # 越界（`..` / 绝对路径）一律拒绝 —— 与工作区沙箱同一精神
        if candidate != root and root not in candidate.parents:
            return None, f"path escapes the skill directory: {inner}"
        if not candidate.is_file():
            return None, f"file not found: {inner}"
        return candidate, ""

    # ── 读 ──────────────────────────────────────────────────────────────

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        skill, inner, error = self._resolve(path)
        if error:
            return ReadResult(path=path, error=error)
        target, error = self._file(skill, inner)
        if target is None:
            return ReadResult(path=path, error=error)
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ReadResult(path=str(target), error=f"read failed: {str(exc)[:120]}")
        # `limit <= 0` = 读**全文原文**（与 workspace 后端同一语义：edit 的一致性校验要用原文）
        if limit <= 0:
            return ReadResult(path=str(target), content=text, total_lines=len(text.splitlines()))
        lines = text.splitlines()
        return ReadResult(
            path=str(target),
            content="\n".join(lines[offset : offset + limit]),
            total_lines=len(lines),
        )

    async def read_bytes(self, path: str) -> ReadBytesResult:
        skill, inner, error = self._resolve(path)
        if error:
            return ReadBytesResult(path=path, error=error)
        target, error = self._file(skill, inner)
        if target is None:
            return ReadBytesResult(path=path, error=error)
        try:
            return ReadBytesResult(path=str(target), data=target.read_bytes())
        except OSError as exc:
            return ReadBytesResult(path=str(target), error=f"read failed: {str(exc)[:120]}")

    async def stat(self, path: str) -> FileStat | None:
        skill, inner, error = self._resolve(path)
        if error:
            return None
        target, error = self._file(skill, inner)
        if target is None:
            return None
        try:
            info = target.stat()
        except OSError:
            return None
        return FileStat(path=str(target), size=info.st_size, is_file=True)

    async def ls(self, path: str = ".") -> LsResult:
        rel = _norm(path)
        if not rel:
            # `/skills/` → 列出技能名（每个技能一个"目录"）
            try:
                entries = [f"{skill.name}/" for skill in _store().list()]
            except Exception as exc:  # noqa: BLE001
                return LsResult(error=f"skill store unavailable: {str(exc)[:120]}")
            return LsResult(entries=entries)
        skill, inner, error = self._resolve(rel)
        if error:
            return LsResult(error=error)
        root = Path(str(getattr(skill, "source_dir", "")))
        target = (root / inner).resolve() if inner else root.resolve()
        if target != root.resolve() and root.resolve() not in target.parents:
            return LsResult(error=f"path escapes the skill directory: {inner}")
        if not target.is_dir():
            return LsResult(error=f"not a directory: {rel}")
        try:
            entries = [
                f"{item.name}/" if item.is_dir() else item.name for item in sorted(target.iterdir())
            ]
        except OSError as exc:
            return LsResult(error=f"list failed: {str(exc)[:120]}")
        return LsResult(entries=entries)

    # ── 不支持（明确报错，而不是静默空结果） ────────────────────────────

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult:
        return GlobResult(error="glob is not supported on /skills/ (use ls or read)")

    async def grep(
        self, pattern: str, *, path: str = ".", max_results: int = 100
    ) -> GrepResult:
        return GrepResult(error="grep is not supported on /skills/ (use read)")

    # ── 只读（硬约束，见模块头） ────────────────────────────────────────

    async def write(self, path: str, content: str) -> WriteResult:
        return WriteResult(path=path, error=_READ_ONLY)

    async def edit(self, path: str, old: str, new: str) -> EditResult:
        return EditResult(path=path, error=_READ_ONLY)

    def supports_execution(self) -> bool:
        """技能目录不是执行面（执行走 `skill_run` 的五类通道）。"""
        return False
