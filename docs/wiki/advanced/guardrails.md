# 高级用法：安全与护栏

> **权威来源**：[Agent 安全与可靠性](../../agent-safety-and-reliability.md)（威胁模型与边界）·
> [工具与审批](../core/tools.md)。**先读威胁模型再看实现** —— 否则容易把护栏加错地方。

## 威胁模型决定护栏形态

**Chiron 的沙箱面对的是「不受信租户」**，不是"保护用户自己的机器"。
这是与单机 harness 的**根本差别**：同一段沙箱代码，保护对象不同，判据就不同。

## 分层隔离（不是"一个沙箱"）

```
① 静态检查    app/tools/code_guard.py      执行前扫代码（AST 级）
② 运行时护栏  app/tools/fs_guard.py        路径
              app/tools/ssrf.py            网络（出站目标）
              app/agent/guards.py          内置函数与工具面
③ 进程隔离    app/tools/sandbox.py · run_code.py · _sandbox_worker.py
④ 审计        app/tools/exec_audit.py      带 tenant/user/session 维度的流水
```

**④ 与前三层同等重要**：没有审计，前三层出事时**查不出是谁做的**。
审计流水是 JSONL，`ts` 统一为 UTC+毫秒。

## 审批：人在回路

不可逆或高影响的操作走**审批**，而不是靠模型自觉。审批决策落审计（`app/agent/approval_audit.py`），
**密钥不落明文**（历史上修过一次明文落盘）。

## 工具授权模式（`toolsMode`）放宽的是什么

`yolo` 之类的模式**放宽审批**，**不绕过护栏**：路径 / 网络 / AST 检查**任何模式下都在**。
两侧（Go `mode.go` / Python `guards.py` / 前端 `ChatView.vue`）由门禁强制对齐。

## 一条尚未拍板的安全相关决定

**`toolsMode` 跨会话切换时是否复位** —— 两种注释曾互相矛盾，代码目前跟随
「仅由用户显式设置改变」。这属于**产品决定**，已记录待确认。

## 延伸阅读

- [工具与审批](../core/tools.md) · [钩子协议](hooks.md)
- [生产：错误码](../production/error-codes.md) —— 被护栏拦下时对外怎么表达
