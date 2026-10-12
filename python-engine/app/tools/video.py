"""视频理解工具（`video_analyze`）—— 抽帧 + 多模态模型描述。

## 为什么是「工具化」而不是「内联」

Chiron 的工具结果是**文本**进上下文（`runtime._sanitize_tool_result` 会 `json.dumps`）——
图像/视频帧**无法**作为 image content block 送给模型。因此视频能力以工具形态落地：

    本地抽帧 → 逐帧交多模态模型 → 返回**带时间戳的文字描述**

这与既有 `vision_analyze` / `file_analyzer` 是同一条取向（媒体一律走工具）。

## 依赖：ffmpeg 二进制（**不引 Python 依赖**）

抽帧需要 `ffmpeg`。本工具**不引入任何 Python 包**（`vendor/规划.md` §1.1），而是运行时探测：

* 探测不到 ⇒ 返回**结构化错误**并写明怎么修，**不静默降级**（§1.3「失败要显式」）；
* 探测到 ⇒ 用**参数数组**起进程（不经 shell ⇒ 路径里的空格/引号不构成注入），
  带超时与帧数上限。

## 审计与机械化归类

这是**模型可触达的起进程路径**（路径参数来自模型），所以与 `shell_exec` / git 工具同记一本账：
写 `exec_audit`。`tests/test_exec_audit_inventory.py` 会枚举 `app/` 下每一处起进程的位置，
未归类即失败 —— 新增本文件时必须同步该测试的 `AUDITED` 表。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from app.tools.registry import registry

logger = logging.getLogger(__name__)

#: 单次分析的帧数上限 —— 每帧都是一次多模态模型调用，直接决定成本与延迟
MAX_FRAMES_CAP = 12
#: 抽帧进程超时（秒）
EXTRACT_TIMEOUT = 60
#: ffmpeg 输出质量（2=最好 … 31=最差；3 在体积与清晰度之间）
JPEG_QSCALE = "3"

DEFAULT_PROMPT = "请描述这一帧的画面内容（对象、场景、可见文字）。用中文简明作答。"


def ffmpeg_binary() -> str | None:
    """探测 ffmpeg 可执行文件；没有返回 None（**不引 Python 依赖**，见模块头）。"""
    return shutil.which("ffmpeg")


def sample_timestamps(*, start: float, duration: float, max_frames: int) -> list[float]:
    """要取样的时间点（秒）—— 纯函数，便于在没有 ffmpeg 的机器上单测。

    * `duration > 0` ⇒ 在该区间内**均匀**取 `max_frames` 个点；
    * `duration <= 0` ⇒ 从 `start` 起**每秒一帧**取前 `max_frames` 帧（调用方可据返回的
      `note` 知道"只覆盖了前 N 秒"）。
    """
    frames = max(1, min(int(max_frames), MAX_FRAMES_CAP))
    if duration > 0:
        step = duration / frames
        return [round(start + step * index, 3) for index in range(frames)]
    return [round(start + index, 3) for index in range(frames)]


def ffmpeg_args(binary: str, source: Path, out_dir: Path, *, start: float, fps: float, max_frames: int) -> list[str]:
    """构造抽帧命令（**参数数组**，不经 shell）。"""
    return [
        binary,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-i", str(source),
        "-vf", f"fps={fps:.6f}",
        "-frames:v", str(max_frames),
        "-q:v", JPEG_QSCALE,
        str(out_dir / "frame_%03d.jpg"),
    ]


def _data_url(frame: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(frame.read_bytes()).decode("ascii")


async def _extract_frames(
    binary: str, source: Path, out_dir: Path, *, start: float, fps: float, max_frames: int
) -> dict[str, Any]:
    """起一次 ffmpeg 抽帧，并按仓库纪律写 `exec_audit`（含超时路径）。"""
    from app.tools.exec_audit import (
        OUTCOME_ERROR,
        OUTCOME_OK,
        OUTCOME_TIMEOUT,
        record_execution,
    )
    from app.tools.sandbox import sandboxed_env

    args = ffmpeg_args(binary, source, out_dir, start=start, fps=fps, max_frames=max_frames)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=sandboxed_env(),  # 清理环境变量，防止宿主密钥泄露给子进程
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=EXTRACT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        record_execution(
            tool="video_analyze",
            command=" ".join(args),
            outcome=OUTCOME_TIMEOUT,
            reason=f"timeout after {EXTRACT_TIMEOUT}s",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {"error": f"ffmpeg timed out after {EXTRACT_TIMEOUT}s"}
    record_execution(
        tool="video_analyze",
        command=" ".join(args),
        outcome=OUTCOME_OK if proc.returncode == 0 else OUTCOME_ERROR,
        exit_code=proc.returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if proc.returncode != 0:
        return {"error": f"ffmpeg failed (exit {proc.returncode}): {stderr.decode('utf-8', 'replace')[:300]}"}
    return {}


async def video_analyze(
    path: str,
    prompt: str = DEFAULT_PROMPT,
    start: float = 0.0,
    duration: float = 0.0,
    max_frames: int = 6,
) -> dict[str, Any]:
    """抽取若干帧并用多模态模型逐帧描述，返回**带时间戳的文字**。

    Args:
        path: 工作区里的视频路径（经 `safe_join` clamp）
        prompt: 每帧的分析提示词
        start: 起始秒
        duration: 覆盖时长（秒）；`0` = 从 `start` 起每秒一帧取前 `max_frames` 帧
        max_frames: 帧数（上限 `MAX_FRAMES_CAP`）
    """
    from app.media.analyzer import analyze_image
    from app.tools.context import get_gateway
    from app.tools.sandbox import safe_join

    # 先做参数校验（越界 / 不存在），再查依赖 —— 否则"路径本就不合法"会被报成
    # "ffmpeg 缺失"，把人引向错误的修法。
    try:
        source = safe_join(path)
    except ValueError:
        return {"error": "path escapes sandbox"}
    if not source.is_file():
        return {"error": f"file not found: {path}"}

    binary = ffmpeg_binary()
    if binary is None:
        return {
            "error": "ffmpeg not found on PATH",
            "hint": (
                "video_analyze needs the ffmpeg binary (no Python dependency is added for this). "
                "Install it in the engine image (e.g. `apt-get install -y ffmpeg`). "
                "Fallback without it: use vision_analyze on a still frame, or file_analyzer."
            ),
        }

    frames = sample_timestamps(start=start, duration=duration, max_frames=max_frames)
    fps = (len(frames) / duration) if duration > 0 else 1.0
    gateway = get_gateway()

    with tempfile.TemporaryDirectory(prefix="chiron_video_") as tmp:
        out_dir = Path(tmp)
        failed = await _extract_frames(
            binary, source, out_dir, start=start, fps=fps, max_frames=len(frames)
        )
        if failed:
            return failed
        extracted = sorted(out_dir.glob("frame_*.jpg"))
        if not extracted:
            return {"error": "no frames extracted (unsupported or corrupt video?)"}

        analyzed: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, frame in enumerate(extracted):
            result = await analyze_image(_data_url(frame), prompt=prompt, gateway=gateway)
            timestamp = frames[index] if index < len(frames) else round(start + index, 3)
            if result.get("success"):
                analyzed.append(
                    {"timestamp_seconds": timestamp, "description": str(result.get("analysis", ""))}
                )
            else:
                errors.append(f"frame@{timestamp}s: {result.get('error', 'unknown error')}")

    out: dict[str, Any] = {
        "path": str(source),
        "frames_total": len(analyzed),
        "frames": analyzed,
    }
    if duration <= 0:
        out["note"] = (
            f"duration 未给：只覆盖了前 {len(frames)} 秒（每秒 1 帧）。"
            "需要覆盖全片请传 duration。"
        )
    if errors:
        out["errors"] = errors
    return out


registry.register(
    name="video_analyze",
    description=(
        "Sample frames from a video (needs ffmpeg in the image) and describe each frame with a "
        "vision model, returning time-stamped text. Use this to answer questions about video content."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Video path relative to the workspace root"},
            "prompt": {"type": "string", "description": "Per-frame analysis prompt"},
            "start": {"type": "number", "default": 0, "description": "Start offset in seconds"},
            "duration": {
                "type": "number",
                "default": 0,
                "description": "Seconds to cover (0 = first max_frames seconds, 1 fps)",
            },
            "max_frames": {
                "type": "integer",
                "default": 6,
                "description": f"Frames to sample (max {MAX_FRAMES_CAP})",
            },
        },
        "required": ["path"],
    },
    handler=video_analyze,
)
