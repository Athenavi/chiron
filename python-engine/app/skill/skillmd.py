"""目录型技能（`SKILL.md` + frontmatter）解析器 —— 对位 deepagents 的技能载体。

Chiron 既有技能载体是**扁平**的 `{name}.skill.json`（`exec`/`parameters` 等字段）；本模块
补上 deepagents 的**目录型**载体：`{dir}/SKILL.md`（frontmatter + 正文 + 同目录附件）。
两者由 `SkillStore` **双读**（同目录下 `.skill.json` 优先，见 store.py 的 `_iter_search`）。

设计要点与「为什么」：

- **`name` 必须等于目录名**：deepagents 以目录名做技能标识。若允许 frontmatter 里的
  `name` 与目录名不同，就会出现「路径解析出来的名字」与「技能自称的名字」两个真相 ——
  覆盖判定（用户同名覆盖内置）会因此错位，所以强制一致，不一致即视为非法并跳过。
- **非法即跳过 + 告警（不静默）**：坏技能静默消失时，「我明明放了技能却不生效」无从排查。
  因此解析失败一律抛 `SkillMdError`，由调用方 `SkillStore` 捕获后 `logger.warning`。
- **上限**：`description ≤1024`、`compatibility ≤500`、**单文件 ≤10MB** —— 直接对齐
  deepagents 的校验，避免超长字段吃上下文 / 超大附件吃磁盘。
- **不引第三方依赖**：frontmatter 是 YAML 子集，而仓库依赖清单里没有 pyyaml，这里手写最小
  解析器（标量 / 引号 / 列表 / 一层嵌套映射），足够覆盖 deepagents 的字段形状。

执行语义：目录型技能是「文档」，没有 `exec` 段；执行时把**正文**包成一次 prompt（与
方案 02 D7 的「显式调用」同源），故 `load_skill_dir` 产出的 `SkillDef.exec_type="prompt"`。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from app.skill.store import SkillDef

logger = logging.getLogger(__name__)

# frontmatter 字段上限（对齐 deepagents 的校验）
MAX_DESCRIPTION_CHARS = 1024
MAX_COMPATIBILITY_CHARS = 500
# 单个文件（SKILL.md 或任一附件）的体积上限：10MB
MAX_FILE_BYTES = 10 * 1024 * 1024

#: frontmatter 围栏
_FRONTMATTER_DELIM = "---"

#: 内置技能目录的环境变量覆盖（部署时仓库根不可见 / 测试注入用）
BUILTIN_SKILLS_ENV = "CHIRON_BUILTIN_SKILLS_PATH"

#: 已告警过的缺失路径（同一路径只警一次，避免每次建 SkillStore 都刷屏）
_MISSING_WARNED: set[str] = set()


class SkillMdError(ValueError):
    """SKILL.md 目录型技能非法（frontmatter 缺失 / 字段越界 / name 与目录名不符）。"""


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆分 SKILL.md 的 frontmatter 与正文，返回 `(映射, 正文)`。

    frontmatter 必须是文件开头的一对 `---` 围栏之间的 YAML 子集；结构非法抛
    `SkillMdError`（由调用方记录告警，不静默）。
    """
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = norm.split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        raise SkillMdError("frontmatter must start with '---' on the first line")
    end = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == _FRONTMATTER_DELIM),
        None,
    )
    if end is None:
        raise SkillMdError("frontmatter is not closed by a second '---'")
    mapping = _parse_block(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return mapping, body


def _parse_block(lines: list[str]) -> dict[str, Any]:
    """解析一层 `key: value`；值为空时收集缩进更深的续块（列表或嵌套映射）。"""
    result: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if ":" not in stripped:
            raise SkillMdError(f"invalid frontmatter line (expected 'key: value'): {raw!r}")
        indent = len(raw) - len(raw.lstrip(" "))
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key:
            raise SkillMdError(f"invalid frontmatter key: {raw!r}")
        value = value.strip()
        if value:
            result[key] = _coerce(_strip_quotes(value))
            i += 1
            continue
        # 空值：收集缩进更深的后续行（列表或嵌套映射）
        j = i + 1
        block: list[str] = []
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip():
                block.append(nxt)
                j += 1
                continue
            if len(nxt) - len(nxt.lstrip(" ")) <= indent:
                break
            block.append(nxt)
            j += 1
        result[key] = _parse_nested(block)
        i = j
    return result


def _parse_nested(block: list[str]) -> Any:
    """解析续块：全为 `- ` 前缀 → 列表；否则当嵌套映射；空块 → None。"""
    entries = [b for b in block if b.strip()]
    if not entries:
        return None
    if entries[0].strip().startswith("-"):
        items: list[Any] = []
        for b in entries:
            s = b.strip()
            if not s.startswith("-"):
                raise SkillMdError(f"mixed list/mapping in frontmatter block: {b!r}")
            items.append(_coerce(_strip_quotes(s[1:].strip())))
        return items
    min_indent = min(len(b) - len(b.lstrip(" ")) for b in entries)
    return _parse_block([b[min_indent:] for b in block])


def _strip_quotes(value: str) -> str:
    """去掉成对的单/双引号（frontmatter 里描述常带引号）。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _coerce(value: str) -> Any:
    """把裸标量转成 bool/int/float/null，否则保留字符串。"""
    low = value.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _normalize_allowed_tools(value: Any) -> list[str]:
    """`allowed-tools` 兼容字符串（逗号分隔）与列表两种写法。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        return [str(t) for t in value if str(t).strip()]
    return [str(value)]


def _collect_attachments(skill_dir: Path) -> list[str]:
    """收集目录内除 `SKILL.md` 之外的附件（相对路径，posix 风格），并校验单文件上限。"""
    attachments: list[str] = []
    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file() or p.name == "SKILL.md":
            continue
        size = p.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SkillMdError(
                f"attachment {p.relative_to(skill_dir)} exceeds {MAX_FILE_BYTES} bytes"
            )
        attachments.append(p.relative_to(skill_dir).as_posix())
    return attachments


def load_skill_dir(skill_dir: Path) -> SkillDef:
    """解析一个目录型技能（`{dir}/SKILL.md`）为 `SkillDef`。

    校验（任一不通过抛 `SkillMdError`，由调用方告警跳过）：

    - frontmatter 合法且含 `name` / `description`；
    - `name` 必须等于目录名；
    - `description ≤1024` 字符、`compatibility ≤500`；
    - `SKILL.md` 与各附件单文件 `≤10MB`。

    正文（frontmatter 之后）作为 prompt 技能的模板源 —— 目录型技能即「文档」，执行时把
    正文包成一次 LLM 调用。
    """
    md_path = skill_dir / "SKILL.md"
    if not md_path.is_file():
        raise SkillMdError(f"SKILL.md not found in {skill_dir}")
    size = md_path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise SkillMdError(f"SKILL.md exceeds {MAX_FILE_BYTES} bytes ({size})")
    frontmatter, body = parse_frontmatter(md_path.read_text(encoding="utf-8"))

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name:
        raise SkillMdError("frontmatter 'name' is required and must be a string")
    if name != skill_dir.name:
        raise SkillMdError(
            f"frontmatter name {name!r} must equal directory name {skill_dir.name!r}"
        )

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description:
        raise SkillMdError("frontmatter 'description' is required and must be a string")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillMdError(
            f"description exceeds {MAX_DESCRIPTION_CHARS} chars ({len(description)})"
        )

    compatibility = frontmatter.get("compatibility", "")
    if compatibility is None:
        compatibility = ""
    if not isinstance(compatibility, str):
        compatibility = str(compatibility)
    if len(compatibility) > MAX_COMPATIBILITY_CHARS:
        raise SkillMdError(
            f"compatibility exceeds {MAX_COMPATIBILITY_CHARS} chars ({len(compatibility)})"
        )

    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {} if metadata is None else {"value": metadata}

    license_value = frontmatter.get("license", "")
    if license_value is None:
        license_value = ""

    return SkillDef(
        name=name,
        description=description,
        exec_type="prompt",
        source=body,
        license=str(license_value),
        compatibility=compatibility,
        allowed_tools=_normalize_allowed_tools(frontmatter.get("allowed-tools")),
        metadata=metadata,
        attachments=_collect_attachments(skill_dir),
        source_dir=str(skill_dir),
    )


def iter_skill_dirs(root: Path) -> list[Path]:
    """列出 `root` 下含 `SKILL.md` 的一级子目录（目录型技能）。"""
    if not root.is_dir():
        return []
    return [
        d for d in sorted(root.iterdir()) if d.is_dir() and (d / "SKILL.md").is_file()
    ]


def builtin_skills_root() -> Path | None:
    """内置技能源目录（对位 deepagents 的 `built_in_skills`）。

    默认从本文件反推仓库根的 `market/skills/`：本文件在 `python-engine/app/skill/` 下，
    故 `parents[3]` 即仓库根。可用 `CHIRON_BUILTIN_SKILLS_PATH` 覆盖（部署时只发布
    `python-engine/`，或测试注入）。

    缺失时返回 `None`（**不抛异常** —— 内置源缺失不该让技能列表报错），但会**告警一次**：
    静默返回 None 曾经掩盖了一个真实缺陷 —— 引擎镜像只 `COPY app/`（构建上下文是
    `python-engine/`），`market/skills/` 进不了镜像，于是容器部署**一直没有内置技能**
    且日志里什么都看不到（vendor/规划.md §1.3「失败要显式」）。
    """
    override = os.getenv(BUILTIN_SKILLS_ENV, "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_dir():
            return candidate
        _warn_missing_source(candidate, explicit=True)
        return None
    candidate = Path(__file__).resolve().parents[3] / "market" / "skills"
    if candidate.is_dir():
        return candidate
    _warn_missing_source(candidate, explicit=False)
    return None


def _warn_missing_source(path: Path, *, explicit: bool) -> None:
    """内置技能源缺失：必须可见，不能静默变成"没有内置技能"。"""
    key = str(path)
    if key in _MISSING_WARNED:
        return
    _MISSING_WARNED.add(key)
    reason = (
        f"{BUILTIN_SKILLS_ENV} 指向的目录不存在"
        if explicit
        else "默认内置技能源（仓库根的 market/skills）不存在"
    )
    logger.warning(
        "内置技能源缺失：%s（%s）—— 本次运行的技能列表**不含内置技能**。"
        "容器部署请挂载 market/skills 并设置 %s 指向它。",
        path, reason, BUILTIN_SKILLS_ENV,
    )
