"""思考档位（reasoning effort）的归一化 —— 只发送对方**认**的值。

## 背景（真实故障）

`effort=medium` 发给 `deepseek-flash` 会得到 `UNSUPPORTED_REASONING_EFFORT`。
根因是各 provider 对档位的词表与取值并不一致：

* 各家表示"**关闭**思考"的字面值有十来种：`disabled` / `false` / `no` / `none` /
  `nothink` / `no-think` / `no_think` / `off` …（ZCode 的 `thoughtLevelOptions.ts`
  把这些**全部归一**到同一档，正是为了绕开同一个坑）；
* SDK（OpenAI 兼容）认的档位是 `minimal / low / medium / high` ——
  **没有 `max`**，也**没有统一的"关"**。

## 策略

**归一化后能映射到 SDK 词表才发；映射不了就不发这个字段** ——
宁可不发（退化为 provider 默认），也不要发一个对方必然拒绝的值。
表示"关闭思考"时**返回空串**：显式发 `"none"` 同样可能 400。
"""

from __future__ import annotations

#: 表示"关闭思考"的写法 → 统一为**不发送**
_OFF = frozenset(
    {"disabled", "false", "no", "none", "nothink", "no-think", "no_think", "off", "0", "closed"}
)

#: 表示"开启（默认强度）"的写法
_ON = frozenset({"enable", "enabled", "on", "true", "yes", "1", "auto", "default"})

#: 我们的档位名 → SDK 档位名（`max` / `highest` 在 SDK 里没有，降到 `high`）
_ALIASES = {"max": "high", "highest": "high", "lowest": "low", "min": "minimal"}

_SDK_LEVELS = frozenset({"minimal", "low", "medium", "high"})


def normalize_reasoning_effort(raw: object) -> str:
    """把任意写法归一化成可安全发送的 SDK 档位。

    返回**空串**表示"不要发送该字段"（关闭思考、或值无法识别）。
    """
    if not isinstance(raw, str):
        return ""
    value = raw.strip().lower()
    if not value:
        return ""
    if value in _OFF:
        return ""
    if value in _ON:
        return "medium"
    value = _ALIASES.get(value, value)
    return value if value in _SDK_LEVELS else ""
