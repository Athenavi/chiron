# 核心组件：工具与审批

> **权威来源**：[Agent 安全与可靠性](../../agent-safety-and-reliability.md)（威胁模型与边界）·
> [会话运行时 spec](../../session-runtime-spec.md)（工具授权模式）。本页只给「工具面在哪、策略怎么对齐」。

## 工具面（引擎侧 `python-engine/app/tools/`，39 个 .py）

| 类别 | 文件 |
|---|---|
| 文件与核心 | `core.py` |
| 执行 | `run_code.py` · `sandbox.py` · `terminal.py` |
| 隔离与审计 | `code_guard.py` · `fs_guard.py` · `ssrf.py` · `exec_audit.py` |
| 媒体与技能 | `media.py` · `skill.py` |
| 记忆与图 | `memory.py` · `graph.py` |
| 浏览器与后台 | `browser.py` · `jobs.py` |
| 委派 | `subagent.py` |

**执行隔离是分层的**（不是"一个沙箱"）：静态 AST 检查 → 运行时 builtins/路径/网络护栏 →
进程级沙箱 → **审计流水**（`exec_audit.py` 带 tenant/user/session 维度）。

## 工具策略：两侧必须同构

**策略表在 Go 与 Python 各有一份，且由门禁强制对齐**：

```
internal/api/tool_policy.go        ←→  python-engine/app/agent/tool_policy.py
internal/api/mode.go               ←→  python-engine/app/agent/guards.py
frontend-vue/src/views/ChatView.vue（工具授权模式的前端一面）
```

`python scripts/check_tool_policy_parity.py` 守这些族：**分级表 / 对话模式 / 提供商目录 /
故障注入 / 共享键前缀 / 共享常量 / 工具授权模式**。**只改一侧 ⇒ 门禁红。**

## 审批（人在回路）

- 引擎侧：`app/agent/approval_audit.py` —— 审批决策**落审计流水**，
  且**密钥不落明文**（历史上修过一次明文落盘）。
- 网关侧：审批票据随会话事件流下发，前端渲染审批卡。
- **审批是"人在回路"的落点**：不可逆或高影响的操作走审批，而不是靠模型自觉。

## 工具授权模式（`toolsMode`）

`yolo` 之类的模式**放宽的是审批**，不是绕过护栏 —— 护栏（路径/网络/AST）**任何模式下都在**。
⚠ 一条**未决的产品决定**：`toolsMode` **跨会话切换时是否复位**（安全相关，两种注释曾互相矛盾，
代码跟随"仅由用户显式设置改变"）。

## 延伸阅读

- [安全与护栏](../advanced/guardrails.md) · [子 Agent](subagents.md)
- [错误码](../production/error-codes.md) —— 工具失败怎么表达
