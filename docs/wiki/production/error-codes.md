# 生产使用：错误码

> **权威来源**：[错误码契约](../../error-codes.md)（**唯一权威**，含三语言键集要求）。
> **有门禁**：`scripts/check_error_code_keys.py`。

## 为什么错误码要立契约

错误码是**跨语言**的：Go 产生 → 前端展示 → 用户看到。三处任何一处漏了，
就会出现"后端说 `quota_exceeded`，界面显示未知错误"。

## 三方必须一致

```
internal/api/error_codes.go              Go 常量（Code*）
frontend-vue/src/locales/zh-CN/errors.ts 三语言键集（zh-CN / en-US / ar）
frontend-vue/src/locales/{en-US,ar}/errors.ts
```

门禁要求：**Go 的 `Code*` ↔ 三语言 `errors.ts` 键集一致**，且**三语言键集彼此相同**。
新增错误码的清单见 [error-codes.md](../../error-codes.md)：**先写 zh-CN，再补 en-US / ar**。

## 从后端文案到错误码的映射

契约里维护了一张**文案 → 错误码**的映射表（因为历史上有按文案匹配的兜底路径）。
所以**改后端错误文案可能改变前端错误码** —— 改之前先看那张表。

表里的每一行都标注了**产生点**（`文件:行号`）。这些行号是**会漂移的**，仓库提供了一个
复核工具：

```bash
python scripts/check_doc_line_refs.py    # 只报"行号超出文件长度"这种零假阳性硬错误
```

## 前端专用键

有些键**后端不产生**（由客户端网络层产生）—— 契约里单独标了"前端专用"一行，
门禁会把它排除在"后端必须产生"之外。

## 延伸阅读

- [可观测性](observability.md) —— 错误计数
- [工具与审批](../core/tools.md) —— 工具失败怎么落到错误码
