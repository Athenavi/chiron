"""网关路由的引擎依赖统计：给"本地模式端点覆盖率"提供可复测的数字。

只读源码，不启动任何进程。用法：

    python scripts/check_endpoint_coverage.py

判据（刻意保守，宁可少算"依赖引擎"）：
1) 该行直接出现 ``pythonClient``；
2) 该行用了 ``newProxy(...)`` 产出的转发器（graphP/kbP/chatP/...）；
3) 该行调用了 ``ForwardRequest``。

这些路由在 python-engine 缺席时统一回 ``python engine not available``，
因此"依赖引擎的比例"就是"本地模式下不可用的比例"。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API = REPO / "internal" / "api"

HANDLE = re.compile(r'mux\.Handle\("(GET|POST|PUT|PATCH|DELETE)\s+(/v1/[^"]*)"')
PROXY_VARS = re.compile(r"\b(graphP|kbP|chatP|capP|memP|mediaP|pluginP|skillP|agentP|uploadP)\s*\(")


def scan_routes() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(API.glob("routes_*.go")):
        if path.name.endswith("_test.go"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in HANDLE.finditer(text):
            method, route = match.group(1), match.group(2)
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.start())
            line = text[start : end if end != -1 else len(text)]
            engine = bool(
                "pythonClient" in line or PROXY_VARS.search(line) or "ForwardRequest" in line
            )
            parts = route.split("/")
            rows.append(
                {
                    "file": path.name,
                    "method": method,
                    "path": route,
                    "group": parts[2] if len(parts) > 2 else "",
                    "engine": engine,
                }
            )
    return rows


def main() -> int:
    rows = scan_routes()
    total = len(rows)
    engine = [r for r in rows if r["engine"]]

    by_group: dict[str, dict[str, int]] = {}
    for r in rows:
        g = by_group.setdefault(r["group"], {"total": 0, "engine": 0})
        g["total"] += 1
        if r["engine"]:
            g["engine"] += 1

    report = {
        "total_routes": total,
        "engine_routes": len(engine),
        "native_routes": total - len(engine),
        "engine_ratio": round(len(engine) / total * 100, 1) if total else 0,
        "by_group": {
            k: {
                **v,
                "native": v["total"] - v["engine"],
                "engine_ratio": round(v["engine"] / v["total"] * 100, 1) if v["total"] else 0,
            }
            for k, v in sorted(by_group.items(), key=lambda kv: -kv[1]["total"])
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
