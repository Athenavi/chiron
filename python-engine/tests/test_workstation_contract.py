"""六大工作台标识的跨端契约测试。

唯一事实源是仓库根的 ``shared/workstations.json``。三端各自手写声明（运行时都不读
那个 JSON），本测试读 JSON 与三端源码比对，锁住漂移：

- Go：``internal/model/workstation.go`` 的 ``WorkstationType`` 常量
- TS：``frontend-vue/src/types/workstation.ts`` 的 ``WORKSTATIONS``
- Python：``app/core/capabilities.py`` 的 ``WorkstationType`` 枚举

背景：此前六个标识是散落在 SQL 字面量与前端字符串里的裸值。Python 侧早就有
``WorkstationType`` 枚举，但 Go 与前端没有，三端各说各话 —— 新增一台时没有任何
编译期或测试期检查。同一入口出现「工作台」与「工作流」两个名字，就是这类漂移的
可见症状之一。

比对范围说明：**id 四端比对**（JSON + Go + TS + Python）；**label 两端比对**
（JSON + TS）—— Go 与 Python 不渲染 UI，没有也不需要展示文案。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.capabilities import WorkstationType

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_JSON = REPO_ROOT / "shared" / "workstations.json"
GO_SOURCE = REPO_ROOT / "internal" / "model" / "workstation.go"
TS_SOURCE = REPO_ROOT / "frontend-vue" / "src" / "types" / "workstation.ts"


def _load_source_of_truth() -> list[dict]:
    data = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
    return data["workstations"]


def _go_ids() -> list[str]:
    """从 Go 常量的字符串字面量里取标识（按文件中的声明顺序）。"""
    text = GO_SOURCE.read_text(encoding="utf-8")
    return re.findall(r'Workstation\w+\s+WorkstationType\s*=\s*"([a-z_]+)"', text)


def _ts_ids() -> list[str]:
    text = TS_SOURCE.read_text(encoding="utf-8")
    block = re.search(
        r"export const WORKSTATIONS\s*=\s*\[(.*?)\]\s*as const", text, re.S
    )
    assert block is not None, "frontend-vue/src/types/workstation.ts 的 WORKSTATIONS 声明找不到"
    return re.findall(r"'([a-z_]+)'", block.group(1))


def _ts_labels() -> dict[str, str]:
    text = TS_SOURCE.read_text(encoding="utf-8")
    block = re.search(r"export const WORKSTATION_LABELS[^=]*=\s*\{(.*?)\n\}", text, re.S)
    assert block is not None, "frontend-vue/src/types/workstation.ts 的 WORKSTATION_LABELS 找不到"
    # label 在 TS 侧走 i18n（`dialogue: t('对话')`），故正则需容忍可选的 t(…) 包裹；
    # 取出的仍是**源语言（zh-CN）原文**，与 shared/workstations.json 比对的口径不变。
    return dict(re.findall(r"([a-z_]+):\s*(?:t\()?'([^']*)'\)?", block.group(1)))


class TestIdsMatchAcrossSources:
    """id 必须四端一致且顺序一致（顺序决定前端展示次序）。"""

    def test_source_of_truth_has_six_workstations(self):
        assert [w["id"] for w in _load_source_of_truth()] == [
            "dialogue",
            "agent",
            "workflow",
            "skill",
            "knowledge",
            "plugin",
        ]

    def test_go_matches_source_of_truth(self):
        assert _go_ids() == [w["id"] for w in _load_source_of_truth()]

    def test_ts_matches_source_of_truth(self):
        assert _ts_ids() == [w["id"] for w in _load_source_of_truth()]

    def test_python_matches_source_of_truth(self):
        assert [w.value for w in WorkstationType] == [w["id"] for w in _load_source_of_truth()]

    def test_all_four_agree_without_duplicates(self):
        expected = [w["id"] for w in _load_source_of_truth()]
        for label, ids in (
            ("go", _go_ids()),
            ("ts", _ts_ids()),
            ("python", [w.value for w in WorkstationType]),
        ):
            assert len(ids) == len(set(ids)), f"{label} 侧存在重复标识"
            assert ids == expected, f"{label} 侧与 shared/workstations.json 漂移"


class TestLabelsMatch:
    """展示文案只在 TS 侧落地，但同样要有唯一基准 —— 否则「工作台/工作流」
    这类同名不同写会重新长出来。"""

    def test_ts_labels_match_source_of_truth(self):
        expected = {w["id"]: w["label"] for w in _load_source_of_truth()}
        assert _ts_labels() == expected

    def test_every_workstation_has_a_label(self):
        missing = set(_ts_ids()) - set(_ts_labels())
        assert not missing, f"缺少展示名: {sorted(missing)}"


class TestSourceFilesExist:
    """事实源与三端声明文件必须都在 —— 文件被误删时给出明确失败，而不是静默跳过。"""

    def test_files_present(self):
        for path in (SOURCE_JSON, GO_SOURCE, TS_SOURCE):
            assert path.is_file(), f"缺少契约文件: {path}"
