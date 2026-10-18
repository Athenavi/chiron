"""`video_analyze`（切口 1 / P2-②）：抽帧 + 多模态模型描述。

本机**没有 ffmpeg**（引擎镜像也刚加上它），所以这组用例钉的是**不需要真实抽帧**的部分：
参数校验顺序、依赖缺失时的结构化错误、采样与命令构造（纯函数）、动作分级。
真实抽帧的端到端需要在装了 ffmpeg 的环境验证（切口 1；那份对照分析文档**不在仓库里**）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.tools  # noqa: F401 — 工具注册的唯一入口（见 app/tools/__init__.py）
from app.tools import video
from app.tools.registry import registry
from app.tools.sandbox import workspace_dir
from app.tools.video import (
    MAX_FRAMES_CAP,
    ffmpeg_args,
    sample_timestamps,
    video_analyze,
)


def test_video_analyze_is_registered():
    assert "video_analyze" in registry.list_names()


def test_video_analyze_is_a_read_level_action():
    """只读动作（读文件 + 调模型），与 `vision_analyze` 同级 —— 两侧分级表必须同构。"""
    from app.agent import tool_policy

    assert tool_policy._TOOL_LEVELS["video_analyze"] == tool_policy.READ


def test_sample_timestamps_are_evenly_spread_when_duration_is_given():
    assert sample_timestamps(start=10.0, duration=10.0, max_frames=5) == [
        10.0,
        12.0,
        14.0,
        16.0,
        18.0,
    ]


def test_sample_timestamps_fall_back_to_one_per_second():
    """`duration` 未给 ⇒ 从 start 起每秒一帧（返回值里会附 note 说明覆盖范围）。"""
    assert sample_timestamps(start=2.0, duration=0.0, max_frames=3) == [2.0, 3.0, 4.0]


def test_sample_timestamps_are_capped():
    ts = sample_timestamps(start=0.0, duration=100.0, max_frames=999)
    assert len(ts) == MAX_FRAMES_CAP, "每帧都是一次模型调用，必须有上限"


def test_ffmpeg_args_are_a_parameter_array():
    """参数数组（不经 shell）—— 路径里的空格/分号不构成注入。"""
    source = Path("/w/a b;rm -rf.mp4")
    args = ffmpeg_args("ffmpeg", source, Path("/tmp/out"), start=5.0, fps=0.5, max_frames=4)

    assert args[0] == "ffmpeg"
    assert "-frames:v" in args and "4" in args
    # 用 `str(source)` 而不是字面量：Path 会按平台归一化分隔符（Windows 上是 `\`）
    assert str(source) in args, "路径必须是**独立元素**，不能被拼进一条 shell 串"


@pytest.mark.asyncio
async def test_missing_ffmpeg_is_a_structured_error(monkeypatch):
    """缺依赖要**显式失败并写出怎么修**，而不是静默降级成"没有内容"。"""
    monkeypatch.setattr(video, "ffmpeg_binary", lambda: None)
    probe = workspace_dir() / "video_probe.tmp"
    probe.write_text("x", encoding="utf-8")
    try:
        out = await video_analyze("video_probe.tmp")
    finally:
        probe.unlink(missing_ok=True)

    assert "ffmpeg" in out["error"]
    assert "hint" in out


@pytest.mark.asyncio
async def test_path_checks_run_before_the_dependency_check():
    """参数校验先于依赖检查 —— 否则"路径不合法"会被误报成"缺 ffmpeg"。"""
    assert (await video_analyze("../../etc/passwd")).get("error") == "path escapes sandbox"
    assert "not found" in (await video_analyze("no-such-video.mp4")).get("error", "")
