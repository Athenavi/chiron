"""D4：用 LLM 生成技能（`SKILL.md`）—— 生成 → **规则校验** →（可选）落盘 → 复查。

三条设计：

1. **生成物不执行、不静默落盘**：先按 D1 的规则校验（复用 `app/skill/skillmd.py` 的解析器与
   上限常量），不通过一律拒绝并给出**具体原因** —— "生成失败"这四个字对使用者没有价值。
2. **落盘后复查 + 失败回滚**：写进目录后用 `load_skill_dir()` 再读一遍 —— 那才是"引擎真的能
   加载它"的证据（格式对但引擎读不了的情况是存在的，例如附件超限）。复查不过就删掉整个目录：
   宁可什么都不留，也不留半个坏技能。
3. **两条调用路径共用**：`tools/skill.py` 的工具 与 `api/skills.py` 的 HTTP 端点此前各自实现了
   一份"拼 SkillDef"的假生成（都没调 LLM）—— 典型双实现漂移风险。现在都调这里。

**无 LLM 时明确报错**，不降级为"模板生成"：那会把"模型没配好"伪装成"技能已生成"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 生成正文的 token 上限（够写一份技能，又不至于烧钱）
DEFAULT_MAX_TOKENS = 1200
#: 引擎侧的名字长度上限（与 `SkillStore.save_skill_md` 的安全校验一致）
MAX_NAME_CHARS = 64
#: 从描述推名字时的长度上限
SLUG_MAX_CHARS = 32


@dataclass
class GenerateOutcome:
    """一次生成的结果（`ok=False` 时 `error` 一定有内容）。"""

    ok: bool
    name: str = ""
    text: str = ""
    skill: dict[str, Any] = field(default_factory=dict)
    path: str = ""
    error: str = ""


def slugify(description: str) -> str:
    """从描述推一个候选技能名（调用方可以显式给 `name` 覆盖它）。

    只保留 `[A-Za-z0-9_-]`，其余折成 `_` —— 与 `SkillStore` 的安全字符集一致，因此生成的
    名字一定落得下去。
    """
    raw = (description or "").strip().lower()
    out = []
    for ch in raw:
        if ch.isascii() and (ch.isalnum() or ch in "-_"):
            out.append(ch)
        elif ch in " \t\r\n":
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    slug = slug[:SLUG_MAX_CHARS].strip("_")
    return slug or "generated_skill"


def _name_is_safe(name: str) -> bool:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    return bool(name) and len(name) <= MAX_NAME_CHARS and set(name) <= allowed and not name.startswith(".")


def extract_markdown(text: str) -> str:
    """从模型输出里取出 SKILL.md 正文。

    模型常把内容包在 ```markdown 围栏（或纯 ``` 围栏）里并附一句解释 —— 直接按原文写盘会
    连围栏一起存进去，frontmatter 就不在文件开头了（D1 的解析器会判非法）。
    """
    raw = (text or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    # 丢掉首行围栏（可能是 ``` / ```markdown），以及末尾的闭合围栏
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


async def _llm_generate(*, name: str, description: str, model: str, max_tokens: int) -> str:
    """一次 LLM 调用产出 SKILL.md 文本；失败返回空串（由调用方报明确错误）。"""
    from app.main import get_gateway

    try:
        gateway = await get_gateway()
    except Exception:  # noqa: BLE001 — 引擎未初始化
        gateway = None
    if gateway is None:
        logger.warning("skill generate: LLM gateway unavailable")
        return ""

    from app.config import settings

    prompt = [
        {
            "role": "system",
            "content": (
                "You write agent skills as ONE SKILL.md file. Reply with the file content ONLY "
                "(no commentary, no code fences). The file MUST begin with a YAML frontmatter "
                "block delimited by '---' lines, containing at least `name` and `description`. "
                f"Use exactly this name: {name}. Keep `description` under 1024 characters. "
                "Then write a concise, actionable body (what the skill does, when to use it, "
                "the steps to follow)."
            ),
        },
        {
            "role": "user",
            "content": f"name: {name}\ndescription: {description}\n\nWrite the SKILL.md now.",
        },
    ]
    try:
        response = await gateway.chat(
            messages=prompt, model=model or settings.default_model, max_tokens=max_tokens
        )
    except Exception as exc:  # noqa: BLE001 — 生成失败以结构化结果返回
        logger.info("skill generate: LLM call failed: %s", str(exc)[:160])
        return ""
    return str(getattr(response, "content", "") or "")


async def generate_skill_md(
    *,
    description: str,
    name: str = "",
    install: bool = False,
    store: Any = None,
    model: str = "",
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> GenerateOutcome:
    """生成一份技能；`install=True` 时落盘为目录型技能并**复查**（失败回滚）。

    `store` 由调用方传入（技能的身份 / 写目标由调用方决定，本模块不猜）。
    """
    text_description = (description or "").strip()
    if not text_description:
        return GenerateOutcome(ok=False, error="description is required")

    skill_name = (name or "").strip() or slugify(text_description)
    if not _name_is_safe(skill_name):
        return GenerateOutcome(ok=False, error=f"invalid skill name: {skill_name!r}")

    raw = await _llm_generate(
        name=skill_name, description=text_description, model=model, max_tokens=max_tokens
    )
    generated = extract_markdown(raw)
    if not generated:
        return GenerateOutcome(ok=False, name=skill_name, error="LLM returned no content")

    # ── 规则校验（复用 D1 的解析器与上限，避免两套 frontmatter 口径）──
    from app.skill.skillmd import MAX_DESCRIPTION_CHARS, SkillMdError, parse_frontmatter

    try:
        meta, body = parse_frontmatter(generated)
    except SkillMdError as exc:
        return GenerateOutcome(
            ok=False, name=skill_name, text=generated, error=f"invalid frontmatter: {exc}"
        )

    declared = str(meta.get("name") or "").strip()
    if declared != skill_name:
        # 与 `load_skill_dir` 同一条硬规则：frontmatter 的 name 必须等于目录名
        return GenerateOutcome(
            ok=False,
            name=skill_name,
            text=generated,
            error=f"frontmatter name {declared!r} != requested {skill_name!r}",
        )
    declared_description = str(meta.get("description") or "").strip()
    if not declared_description:
        return GenerateOutcome(
            ok=False, name=skill_name, text=generated, error="frontmatter is missing description"
        )
    if len(declared_description) > MAX_DESCRIPTION_CHARS:
        return GenerateOutcome(
            ok=False,
            name=skill_name,
            text=generated,
            error=f"description too long ({len(declared_description)} > {MAX_DESCRIPTION_CHARS})",
        )
    if not body.strip():
        return GenerateOutcome(ok=False, name=skill_name, text=generated, error="body is empty")

    outcome = GenerateOutcome(ok=True, name=skill_name, text=generated)
    outcome.skill = {
        "name": skill_name,
        "description": declared_description,
        "version": str(meta.get("version") or "0.1.0"),
        "exec": {"type": "prompt", "source": ""},
    }

    if not install or store is None:
        return outcome

    # ── 落盘 + **复查**（引擎真能加载它才算成功）+ 失败回滚 ──
    from app.skill.skillmd import load_skill_dir

    try:
        path = store.save_skill_md(skill_name, generated)
    except Exception as exc:  # noqa: BLE001
        return GenerateOutcome(
            ok=False, name=skill_name, text=generated, error=f"failed to write SKILL.md: {exc}"
        )

    try:
        skill = load_skill_dir(path.parent)
    except Exception as exc:  # noqa: BLE001 — 复查不通过 ⇒ 回滚，不留半个坏技能
        store.delete_skill_dir(skill_name)
        return GenerateOutcome(
            ok=False,
            name=skill_name,
            text=generated,
            error=f"generated skill failed validation on load: {exc}",
        )

    outcome.path = str(path)
    outcome.skill = skill.to_dict()
    return outcome
