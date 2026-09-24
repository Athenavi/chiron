"""Tests for jobs（后台任务）与 read_image（图片读取）。"""
from __future__ import annotations

import asyncio
import base64

import pytest

from app.tools.context import set_tool_context
from app.tools.core import read_image
from app.tools.jobs import job_kill, job_output, run_in_background
from app.tools.sandbox import workspace_dir
from app.tools.terminal import _terminal


class TestJobs:
    @pytest.mark.asyncio
    async def test_background_job_completes(self):
        set_tool_context(session_id="j-sess")
        start = await run_in_background("echo bg-done")
        assert start["status"] == "started"
        job_id = start["job_id"]
        # 轮询直到完成
        for _ in range(50):
            res = await job_output(job_id)
            if res["status"] == "completed":
                break
            await asyncio.sleep(0.1)
        assert res["status"] == "completed"
        assert "bg-done" in res["output"]
        await _terminal.close_all()

    @pytest.mark.asyncio
    async def test_unknown_job(self):
        res = await job_output("job_nope")
        assert "error" in res

    @pytest.mark.asyncio
    async def test_kill_unknown_job(self):
        res = await job_kill("job_nope")
        assert "error" in res


class TestReadImage:
    @pytest.mark.asyncio
    async def test_read_png_as_data_url(self, tmp_path):
        # 1x1 PNG
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        set_tool_context(session_id="s", user_id="u-img", tenant_id="t", gateway=None)
        (workspace_dir() / "pixel.png").write_bytes(png)
        out = await read_image("pixel.png")
        assert out["media_type"] == "image/png"
        assert out["data_url"].startswith("data:image/png;base64,")
        assert out["bytes"] == len(png)

    @pytest.mark.asyncio
    async def test_unsupported_type(self, tmp_path):
        set_tool_context(session_id="s", user_id="u-img", tenant_id="t", gateway=None)
        (workspace_dir() / "doc.txt").write_text("hi", encoding="utf-8")
        out = await read_image("doc.txt")
        assert "error" in out and "unsupported" in out["error"]

    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path):
        set_tool_context(session_id="s", user_id="u-img", tenant_id="t", gateway=None)
        out = await read_image("missing.png")
        assert "error" in out
