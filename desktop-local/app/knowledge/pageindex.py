"""PageIndex 组件探测（只探测，**不启动**）。

契约 §2 里 ``ComponentInfo`` 的判定基准是组件目录：``userData/components/<name>``。
core 的 ``data-dir`` 默认就是 ``userData/local``，所以两个候选目录是：

1. ``<data-dir>/components/pageindex``      —— data-dir 被改到别处时开发者自己放的位置
2. ``<data-dir>/../components/pageindex``   —— 即 ``userData/components/pageindex``

只做"目录在不在"这一件事，不做下载、不做解压、不 spawn 子进程：真正的索引构建属于 P3，
本批的 ``/build`` 只回答"组件装了没有"，装了也给 202 占位。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings

COMPONENT_NAME = "pageindex"
MANIFEST_NAME = "manifest.json"


def component_candidates(settings: Settings) -> list[Path]:
    """按优先级给出候选目录（返回值只用于探测与报错展示，不用于写入）。"""
    data_dir = Path(settings.data_dir)
    return [
        data_dir / "components" / COMPONENT_NAME,
        data_dir.parent / "components" / COMPONENT_NAME,
    ]


def find_pageindex_dir(settings: Settings) -> Path | None:
    """返回第一个存在的候选目录；都没有返回 ``None``。"""
    for candidate in component_candidates(settings):
        if candidate.is_dir():
            return candidate
    return None


def pageindex_status(settings: Settings) -> dict[str, Any]:
    """给 ``/build`` 与日志用的探测结果（``candidates`` 让用户知道该往哪放）。"""
    found = find_pageindex_dir(settings)
    manifest = bool(found is not None and (found / MANIFEST_NAME).is_file())
    return {
        "name": COMPONENT_NAME,
        "installed": found is not None,
        "path": str(found) if found is not None else "",
        "manifest": manifest,
        "candidates": [str(candidate) for candidate in component_candidates(settings)],
    }
