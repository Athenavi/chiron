"""一键复现「Chiron ↔ deepagents」对标的编排脚本（E3）。

## 为什么需要它

`evals/README.md` 已经写清了怎么做对标，但那是**三条命令 + 若干前置**：装依赖、起栈、配 key、
跑两侧、再 compare。任何一步缺失，结果就不成立 —— 而"结果不成立"最危险的表现是**跑出一份
看起来正常的报告**（例如 Chiron 侧根本没起来、报告里全是失败，却被读成"对手更强"）。

所以本脚本把**前置体检放在最前面**：缺什么就**明确失败并给出修复命令**（fail-closed），
并把**固定口径**（固件集 + 固件哈希 + 模型 + repeat）写进结果元数据 —— 这样"可复现"才是
可验证的：拿同样的固件哈希与模型，任何人都能对照。

## 三条路径

    python -m evals.reproduce --check                              # 只体检，零副作用
    python -m evals.reproduce --model gpt-4o-mini --out-dir out    # 端到端（跑两侧 + 对照）
    python -m evals.reproduce --compare a.json b.json              # 已有报告，只对照

## 两条硬约束（与 README 一致）

* **替身（`--submit scripted`）只验证链路、不代表能力** ⇒ 本脚本**不接受**它作为对标输入；
* deepagents 侧依赖（langchain / langgraph）**不在主依赖清单** ⇒ 缺失时提示装到**独立环境**，
  而不是把它塞进主 requirements。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

#: 固定口径：对标用 full 集（含 smoke 里标 full 的任务），单次运行即可比较准确性
DEFAULT_SUITE = "full"
DEFAULT_TIER = "full"
#: 两侧必须用**同一个模型名**，否则对照出来的差异里混着模型差
DEFAULT_MODEL = "gpt-4o-mini"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SUITES_DIR = Path(__file__).resolve().parent / "suites"
_VENDOR_DEEPAGENTS = _REPO_ROOT / "vendor" / "deepagents" / "libs" / "deepagents"


@dataclass(frozen=True)
class Precondition:
    """一条前置条件。`ok=False` 时 `detail` 必须写清**缺什么 + 怎么修**。"""

    name: str
    ok: bool
    detail: str = ""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_fingerprint(suite: str = DEFAULT_SUITE) -> dict[str, str]:
    """固件指纹：同指纹 + 同模型 ⇒ 结果可比。缺文件记为 `""`（由体检报出）。"""
    out: dict[str, str] = {}
    for name in (suite, "smoke"):  # full tier 会并入 smoke 里标 full 的任务
        path = _SUITES_DIR / f"{name}.json"
        out[name] = _sha256(path) if path.exists() else ""
    return out


def check_preconditions(
    *,
    base_url: str,
    api_key: str,
    need_stateful_stack: bool,
    has_deepagents_deps: bool | None = None,
    stack_healthy: bool | None = None,
) -> list[Precondition]:
    """体检。**纯函数**（三个"探测结果"可注入 ⇒ 可单测；端到端时由 main 负责实测）。"""
    if has_deepagents_deps is None:
        has_deepagents_deps = all(
            importlib.util.find_spec(mod) is not None
            for mod in ("deepagents", "langchain_openai")
        )
    checks: list[Precondition] = []

    fingerprint = fixture_fingerprint()
    missing = [name for name, digest in fingerprint.items() if not digest]
    checks.append(
        Precondition(
            "固件可读",
            not missing,
            f"缺少固件文件：{missing}（检查 evals/suites/）",
        )
    )

    checks.append(
        Precondition(
            "deepagents 依赖",
            bool(has_deepagents_deps),
            "缺 deepagents/langchain_openai。**装到独立环境**（不是主依赖清单）：\n"
            f"    pip install -e {_VENDOR_DEEPAGENTS} langchain-openai",
        )
    )

    checks.append(
        Precondition(
            "模型密钥",
            bool(api_key),
            "缺模型密钥。两侧都要用真实模型（替身只验证链路、不代表能力）：\n"
            "    CHIRON_EVAL_LLM_API_KEY=<key> python -m evals.reproduce --model <model>",
        )
    )

    if need_stateful_stack:
        checks.append(
            Precondition(
                "Chiron 栈可达",
                bool(stack_healthy),
                f"网关不可达（{base_url or '未给 --base-url'}）。先起栈：\n"
                "    python run.py start   # 网关 :8080 + 引擎 :8000",
            )
        )
    return checks


def _fail(checks: list[Precondition]) -> int:
    bad = [c for c in checks if not c.ok]
    for c in checks:
        print(f"[{'ok' if c.ok else 'FAIL'}] {c.name}" + (f" — {c.detail}" if not c.ok else ""))
    if bad:
        print(f"\n前置未满足 {len(bad)} 项 —— 已中止（不产出可能被误读的报告）", file=sys.stderr)
        return 1
    return 0


def _run_chiron(*, base_url: str, api_key: str, out: Path, model: str, repeat: int) -> int:
    from evals import cli

    return cli.main(
        [
            "run",
            "--suite", DEFAULT_SUITE,
            "--tier", DEFAULT_TIER,
            "--submit", "http",
            "--base-url", base_url,
            "--api-key", api_key,
            "--agent", "chiron",
            "--repeat", str(repeat),
            "--report", str(out),
        ]
    )


def _run_deepagents(*, out: Path, model: str, repeat: int) -> int:
    """跑 vendor 侧适配器（独立环境 ⇒ 子进程，避免污染本进程的 import）。"""
    proc = subprocess.run(  # noqa: S603 - 参数固定，来自本脚本而非用户输入拼接
        [
            sys.executable,
            "-m",
            "evals.adapters.deepagents_agent",
            "--suite", DEFAULT_SUITE,
            "--tier", DEFAULT_TIER,
            "--model", model,
            "--repeat", str(repeat),
            "--out", str(out),
        ],
        check=False,
    )
    return proc.returncode


def _compare(report_paths: list[Path], out: Path) -> dict[str, object]:
    from evals.cli import _report_from_dict  # noqa: PLC2701 - 复用既有解析，避免两套口径
    from evals.report import compare

    reports = {}
    for path in report_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        reports[str(payload.get("agent", path.name))] = _report_from_dict(payload)
    table = compare(reports)
    out.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
    return table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.reproduce", description="一键复现对标（E3）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="两侧共用的模型名")
    parser.add_argument("--repeat", type=int, default=1, help="每条任务跑几次（pass@k）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--check", action="store_true", help="只体检，不跑")
    parser.add_argument("--compare", nargs="+", default=None, help="只对照已有报告")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    api_key = args.api_key or _key_from_env()

    if args.compare:
        out = out_dir / "compare.json"
        out_dir.mkdir(parents=True, exist_ok=True)
        table = _compare([Path(p) for p in args.compare], out)
        print(json.dumps(table, ensure_ascii=False, indent=2))
        print(f"\n对照表已写入 {out}")
        return 0

    stack_healthy = None if args.check else _stack_healthy(args.base_url)
    checks = check_preconditions(
        base_url=args.base_url,
        api_key=api_key,
        need_stateful_stack=not args.check,
        stack_healthy=stack_healthy,
    )
    code = _fail(checks)
    if args.check:
        return code
    if code != 0:
        return code

    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    chiron_report = out_dir / "chiron-full.json"
    deepagents_report = out_dir / "deepagents-full.json"

    if _run_chiron(
        base_url=args.base_url, api_key=api_key, out=chiron_report,
        model=args.model, repeat=args.repeat,
    ) != 0:
        print("Chiron 侧未全部通过 —— 对照会失真，已中止", file=sys.stderr)
        return 1
    if _run_deepagents(out=deepagents_report, model=args.model, repeat=args.repeat) != 0:
        print("deepagents 侧运行失败 —— 对照会失真，已中止", file=sys.stderr)
        return 1

    table = _compare([chiron_report, deepagents_report], out_dir / "compare.json")
    # 口径元数据：让"可复现"可被外部核对（同固件哈希 + 同模型 ⇒ 结果可比）
    (out_dir / "compare.meta.json").write_text(
        json.dumps(
            {
                "started_at": started,
                "suite": DEFAULT_SUITE,
                "tier": DEFAULT_TIER,
                "model": args.model,
                "repeat": args.repeat,
                "fixture_sha256": fixture_fingerprint(),
                "reports": [str(chiron_report), str(deepagents_report)],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(table, ensure_ascii=False, indent=2))
    print(f"\n对照表 → {out_dir / 'compare.json'} · 口径 → {out_dir / 'compare.meta.json'}")
    return 0


def _key_from_env() -> str:
    import os

    return os.getenv("CHIRON_EVAL_LLM_API_KEY", "") or os.getenv("LLM_API_KEY", "")


def _stack_healthy(base_url: str) -> bool:
    from urllib.error import URLError
    from urllib.request import urlopen

    try:
        with urlopen(f"{base_url.rstrip('/')}/health", timeout=3) as resp:  # noqa: S310 - 本机自检
            return 200 <= resp.status < 300
    except (URLError, OSError):
        return False


if __name__ == "__main__":
    sys.exit(main())
