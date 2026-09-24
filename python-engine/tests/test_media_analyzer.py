"""vision/文件分析路径的网关注入 —— 回归保护。

历史缺陷（本次修复）：``app/media/analyzer.py:analyze_image`` 曾写

    from app.llm.client import get_llm_client

但 **该函数在仓库里从未存在** —— ``app/llm/client.py`` 只有 ``LLMClient``，而且它
只提供 ``embed()``（嵌入），没有 ``chat()``。于是这行必然抛 ImportError，又被函数体
的 ``except Exception`` 吞成 ``{"success": False, "error": ...}``：

* 多模态图片分析从未真正工作过；
* 因为不抛异常、只返回失败字典，故障被静默吸收；
* 而且没有任何测试覆盖这条路径，所以一直没被发现。

本文件锁死正确行为：**gateway 必须显式注入**，注入后必须真的走 ``gateway.chat``，
且多模态消息体（text + image_url）必须原样传下去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.media.analyzer import analyze_image


@dataclass
class _ChatResponse:
    """与 app/gateway/provider.py 的 ChatResponse 形状一致（只取被测字段）。"""

    content: str
    model: str
    finish_reason: str = "stop"
    provider: str = "fake"


@dataclass
class _FakeGateway:
    content: str = "一只橘猫趴在窗台上"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def chat(self, messages, model, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"messages": messages, "model": model, "kwargs": kwargs})
        return _ChatResponse(content=self.content, model=model)


async def test_analyze_image_without_gateway_fails_loudly():
    """未注入 gateway 时必须**明确失败**，而不是静默返回成功或抛 ImportError。"""
    out = await analyze_image("https://example.com/a.png")

    assert out["success"] is False
    # 错误信息要指出该怎么修（提示注入 gateway），而不是一句笼统的 "failed"
    assert "gateway" in out["error"]


async def test_analyze_image_uses_injected_gateway():
    """注入 gateway 后应真正调用 chat，并把多模态消息下发。"""
    gw = _FakeGateway()
    out = await analyze_image("https://example.com/a.png", gateway=gw)

    assert out["success"] is True
    assert out["analysis"] == "一只橘猫趴在窗台上"
    assert out["model"] == gw.calls[0]["model"]

    assert len(gw.calls) == 1
    parts = gw.calls[0]["messages"][0]["content"]
    kinds = {p.get("type") for p in parts}
    assert kinds == {"text", "image_url"}
    # 图片地址要原样传给模型，不能被丢掉
    image_part = next(p for p in parts if p.get("type") == "image_url")
    assert image_part["image_url"]["url"] == "https://example.com/a.png"


async def test_analyze_image_honours_vision_model_env(monkeypatch):
    """模型名跟随 VISION_MODEL（原先这条链路根本走不到，故一并锁住）。"""
    monkeypatch.setenv("VISION_MODEL", "my-vision-model")
    gw = _FakeGateway()
    await analyze_image("https://example.com/a.png", gateway=gw)

    assert gw.calls[0]["model"] == "my-vision-model"


async def test_analyze_image_custom_prompt_is_used():
    """自定义提示词要替换默认提示词（而不是两者都发）。"""
    gw = _FakeGateway()
    await analyze_image("https://example.com/a.png", prompt="只数人数", gateway=gw)

    text_part = next(
        p for p in gw.calls[0]["messages"][0]["content"] if p.get("type") == "text"
    )
    assert text_part["text"] == "只数人数"
