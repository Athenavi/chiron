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
    async def test_background_job_completes(self, monkeypatch):
        set_tool_context(session_id="j-sess")

        # 强制走**本地降级**路径：本机是否有 Redis 决定了 `run_in_background` 的分支 ——
        # 有 Redis 时它会把 job 入队并写 `job:meta:{id}=running`，而 `job_output` 一旦看到
        # meta=running 就**直接返回 running、不再回查本地任务**（app/tools/jobs.py:139-141），
        # 于是在"有 Redis 但没有 worker 消费队列"的机器上（本机就是）该 job 永远 running。
        #
        # 这条用例测的是"后台命令能跑完并拿到输出"（进程内语义），与队列/worker 无关，
        # 因此必须把环境依赖去掉 —— 否则它在本机红、在 CI 绿（CI 的 python job 无 Redis）。
        import app.tools.jobs as jobs_mod

        async def _no_enqueue(*_args: object, **_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(jobs_mod, "_enqueue_tool_job", _no_enqueue)

        start = await run_in_background("echo bg-done")
        assert start["status"] == "started"
        job_id = start["job_id"]
        # 轮询直到完成
        res: dict = {"status": "running"}
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
