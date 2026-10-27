"""硬编码配置 / 路径的**基线扫描**（只读，不因发现而失败）。

扫描范围：

- ``frontend-vue/electron/*.cjs``（不递归，``__tests__/`` 天然不在里面）
- ``desktop-local/app/**/*.py``

它做三件事：

1. 按规则把 ``127.0.0.1`` / ``localhost`` / ``11434`` / ``python`` / ``chiron-local`` /
   绝对路径 / 端口字面量等"写死的配置面"逐条找出来（:class:`Finding`）；
2. 汇总成清单（:func:`summarize` + :func:`render`）——**只输出，不断言为零**。
   基线测试的用途是把现状钉住：今天有 200 条不代表 CI 该红，明天多出 20 条才值得看一眼；
3. 只对"扫描器自身还能用"做断言：目标目录存在、两棵树都扫到了文件、规则名不重复、
   每条命中都能定位到行。扫描器坏掉（glob 改了、文件被挪走）必须失败，这是唯一的红灯。

清单在 ``pytest -q`` 下会被捕获。想直接看：

    pytest tests/test_hardcode_audit.py -q -s
    python -m tests.test_hardcode_audit          # 在 desktop-local/ 下跑

人工结论（哪些该提取、提取到哪里）见 ``docs/desktop-local/configuration.md``。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

#: test_hardcode_audit.py → tests → desktop-local → 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
ELECTRON_DIR = REPO_ROOT / "frontend-vue" / "electron"
APP_DIR = REPO_ROOT / "desktop-local" / "app"

#: 清单里每条只留这么长的片段，避免把整行 traceback 之类刷进报告
SNIPPET_CHARS = 120


@dataclass(frozen=True)
class Rule:
    """一类硬编码特征。``note`` 会原样出现在清单里，说明"这为什么算配置面"。"""

    name: str
    pattern: re.Pattern[str]
    note: str


@dataclass(frozen=True)
class Finding:
    """一次命中：规则、文件（相对仓库根）、行号、该行的片段。"""

    rule: str
    path: str
    line: int
    snippet: str


#: 规则表。刻意用"特征"而不是"白名单/黑名单"：命中不等于有错，
#: 只说明这一行参与了某个配置决定，值得在审计时被看一眼。
RULES: tuple[Rule, ...] = (
    Rule("loopback-ip", re.compile(r"127\.0\.0\.1"), "回环地址字面量（应集中成常量）"),
    Rule("localhost", re.compile(r"localhost"), "localhost 字面量（与 127.0.0.1 混用会掩盖绑定差异）"),
    Rule("ollama-port", re.compile(r"\b11434\b"), "Ollama 默认端口（应由 provider 配置覆盖）"),
    Rule("python-interpreter", re.compile(r"\bpython(?:3|\.exe)?\b"), "解释器名/路径（CHIRON_PYTHON 的兜底值）"),
    Rule("core-binary", re.compile(r"chiron-local"), "core 二进制名 / data-dir 兜底目录名"),
    Rule(
        "abs-path-unix",
        re.compile(r"(?<![\w:/])/(?:usr|opt|etc|var|home|bin|sbin|srv|Applications|System|Library)/"),
        "POSIX 绝对路径",
    ),
    Rule("abs-path-windows", re.compile(r"\b[A-Za-z]:[\\/]"), "Windows 绝对路径 / 盘符"),
    Rule(
        "home-shorthand",
        re.compile(r"~/|\$HOME|%APPDATA%|%LOCALAPPDATA%|AppData|XDG_DATA_HOME"),
        "家目录与环境变量拼接出的路径",
    ),
    Rule("env-var", re.compile(r"\bCHIRON_[A-Z0-9_]+\b"), "CHIRON_* 环境变量引用点（契约名 / core 真名）"),
    Rule("port-literal", re.compile(r":\d{3,5}\b"), "端口字面量（示例端口容易变成事实上的默认值）"),
    Rule(
        "endpoint-path",
        re.compile(r"/(?:healthz|readyz|infer|api/chat|chat/completions|health|components)\b"),
        "HTTP 端点路径",
    ),
    Rule(
        "userdata-path",
        re.compile(r"userData|resourcesPath"),
        "Electron 的 userData / resources 路径拼接点",
    ),
    Rule("file-mode", re.compile(r"0o[0-7]{3}"), "全局可读写的文件权限（凭据文件必须 0o600）"),
)


def iter_scan_files() -> list[Path]:
    """要扫描的文件：electron 顶层 ``*.cjs`` + ``app`` 下所有 ``*.py``（排序保证报告稳定）。"""
    files: list[Path] = []
    if ELECTRON_DIR.is_dir():
        files.extend(sorted(ELECTRON_DIR.glob("*.cjs")))
    if APP_DIR.is_dir():
        files.extend(sorted(APP_DIR.glob("**/*.py")))
    return files


def display_path(path: Path) -> str:
    """相对仓库根的 POSIX 风格路径：报告跨平台一致。"""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:  # 理论上不会发生；真发生了也别让扫描崩掉
        return path.as_posix()


def scan_text(text: str, path: str) -> list[Finding]:
    """对一段文本逐行套规则。同一行命中同一规则多次只记一条。"""
    findings: list[Finding] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        for rule in RULES:
            if not rule.pattern.search(raw):
                continue
            findings.append(
                Finding(
                    rule=rule.name,
                    path=path,
                    line=number,
                    snippet=line[:SNIPPET_CHARS],
                )
            )
    return findings


def scan_file(path: Path) -> list[Finding]:
    """读一个文件并扫描。解码失败按替换处理：审计工具不该因为一个坏字节就停。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    return scan_text(text, display_path(path))


def scan_all() -> list[Finding]:
    """扫描全部目标文件；结果按 (路径, 行号, 规则) 排序。"""
    findings: list[Finding] = []
    for path in iter_scan_files():
        findings.extend(scan_file(path))
    return sorted(findings, key=lambda item: (item.path, item.line, item.rule))


def summarize(findings: list[Finding]) -> list[tuple[str, int, list[tuple[str, int]]]]:
    """按规则分组，返回 ``[(规则名, 命中总数, [(文件, 命中数)...])]``（只含非空规则）。"""
    grouped: dict[str, Counter[str]] = {}
    for finding in findings:
        grouped.setdefault(finding.rule, Counter())[finding.path] += 1

    inventory: list[tuple[str, int, list[tuple[str, int]]]] = []
    for rule in RULES:  # 按规则表顺序输出，报告才稳定
        counts = grouped.get(rule.name)
        if not counts:
            continue
        inventory.append((rule.name, sum(counts.values()), counts.most_common()))
    return inventory


def render(inventory: list[tuple[str, int, list[tuple[str, int]]]]) -> str:
    """把汇总渲染成可粘贴的文本清单。"""
    notes = {rule.name: rule.note for rule in RULES}
    lines = ["硬编码审计清单（基线：发现本身不算失败）", ""]
    for name, total, per_file in inventory:
        lines.append(f"[{name}] {total} 处 —— {notes.get(name, '')}")
        for path, count in per_file:
            lines.append(f"    {count:>4}  {path}")
        lines.append("")
    if not inventory:
        lines.append("（一条都没命中：先确认扫描目标与规则是否已经失效）")
    return "\n".join(lines)


def main() -> int:
    """直接跑时的入口：打印完整清单，退出码恒为 0（基线语义）。"""
    findings = scan_all()
    print(render(summarize(findings)))
    print(f"合计 {len(findings)} 条，扫描 {len(iter_scan_files())} 个文件")
    return 0


# --------------------------------------------------------------------- 断言
# 只有"扫描器还能用"这四件事会失败；命中数量再多也只是一份报告。


def test_scan_targets_exist() -> None:
    """两棵目标树都必须存在，且各自扫到了文件。"""
    assert ELECTRON_DIR.is_dir(), f"缺少 {ELECTRON_DIR}（electron 目录被移走了？）"
    assert APP_DIR.is_dir(), f"缺少 {APP_DIR}（desktop-local/app 被移走了？）"

    files = iter_scan_files()
    suffixes = {path.suffix for path in files}
    assert ".cjs" in suffixes, "没有扫到任何 .cjs（electron 侧）"
    assert ".py" in suffixes, "没有扫到任何 .py（core 侧）"


def test_rules_are_well_formed() -> None:
    """规则名唯一、都有说明、正则都能用（重复名字会让汇总互相覆盖）。"""
    names = [rule.name for rule in RULES]
    assert len(names) == len(set(names)), f"规则名重复：{names}"
    for rule in RULES:
        assert rule.note, f"{rule.name} 缺少说明"
        assert rule.pattern.pattern, f"{rule.name} 是空正则"


def test_scan_text_finds_known_patterns() -> None:
    """喂一段人造文本，确认规则真的会命中（而不是全部静默漏掉）。"""
    sample = "\n".join(
        [
            "const base = 'http://127.0.0.1:11434'",
            "const exe = process.env.CHIRON_PYTHON || 'python'",
            "const bin = 'chiron-local.exe'",
            'const p = "C:\\\\Program Files\\\\PostgreSQL\\\\16\\\\bin"',
            "os.chmod(path, 0o600)",
        ]
    )
    hit = {finding.rule for finding in scan_text(sample, "sample.cjs")}
    expected = (
        "loopback-ip",
        "ollama-port",
        "python-interpreter",
        "core-binary",
        "abs-path-windows",
        "file-mode",
    )
    for name in expected:
        assert name in hit, f"规则 {name} 在样例上没命中"


def test_baseline_report_does_not_fail_on_findings() -> None:
    """基线语义：输出清单，不做"命中数 == 0"之类的断言。"""
    findings = scan_all()
    inventory = summarize(findings)

    report = render(inventory)
    print(report)  # pytest -q -s 可直接看；捕获时也不影响结果

    # 一条都没命中说明规则或目标坏了，而不是"仓库变干净了"
    assert findings, "扫描结果为空：先确认 iter_scan_files() 还有文件可扫"
    # 每条命中都要能定位（文件 + 行号 + 片段），否则清单不可执行
    for finding in findings:
        assert finding.path.endswith((".cjs", ".py")), finding
        assert finding.line >= 1, finding
        assert finding.snippet, finding
    # 汇总只能包含规则表里的名字
    assert {name for name, _total, _files in inventory} <= {rule.name for rule in RULES}


if __name__ == "__main__":
    raise SystemExit(main())
