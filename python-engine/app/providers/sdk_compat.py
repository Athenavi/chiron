"""Provider SDK 参数兼容 —— **只发对方认的参数**。

## 背景（真实故障）

`requirements.txt` 在 b9d9566（"python-engine 依赖升级至最新版"）把两个 provider SDK
一起推到新大版本（`anthropic` 0.52.0 → 1.13.0、`openai` 1.93.0 → 3.29.0）。新 SDK 跟随
上游 API 收紧了方法签名：**Anthropic 已废弃采样参数**，于是 `Messages.stream()` 不再
接受 `temperature` —— 我们照旧透传，拿到的是 **Python 层的 TypeError**，而不是上游 400：

    provider opencode-go-anthropic stream failed:
    TypeError: AsyncMessages.stream() got an unexpected keyword argument 'temperature'

对用户而言，表现是"provider 配好了、模型也选对了，但一发请求就失败"。

## 策略

下发前按目标方法的**实际签名**裁参数：签名里没有的键就地丢弃，并在**首次**丢弃时告警
一次（同一个参数不逐请求刷屏）。签名里有的照发 —— 所以旧 SDK（0.x）的行为完全不变。
这与 `effort.py` 是同一策略：**宁可不发**（退化为服务端默认），也不要发一个对方必然
拒绝的值；不能因为"某个可选参数发不出去"就让整轮对话失败。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 已告警过的参数名（逗号连接的键集合）—— 同一个参数只提醒一次，避免逐请求刷屏。
_warned: set[str] = set()


def drop_unsupported_params(method: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """删掉 ``method`` 签名里不存在的键，返回**同一个** dict（就地修改）。

    签名带 ``**kwargs``、或根本取不到签名（内建对象 / 深度装饰）时一律放行 ——
    那种形态下"认不认这个参数"由 SDK 自己决定，我们不该替它裁。
    """
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):  # 签名不可得 → 不冒险裁
        return kwargs
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs

    dropped = sorted(key for key in kwargs if key not in params)
    for key in dropped:
        del kwargs[key]
    if dropped:
        name = ",".join(dropped)
        if name not in _warned:
            _warned.add(name)
            logger.warning(
                "SDK 方法 %s 不接受参数 %s —— 已省略，退化为服务端默认值"
                "（多为依赖升级后上游废弃了该参数，见 providers/sdk_compat.py）。",
                getattr(method, "__qualname__", method),
                name,
            )
    return kwargs
