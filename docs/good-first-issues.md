# 入门任务（good first issue 候选）

> 这些任务**范围明确、验收可机械判定、不需要架构决策**，适合第一次贡献。
> 维护者可以把每条标题与正文直接贴成 GitHub issue，打上 `good first issue` 标签。
> 动手前先读 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## 1. 给 `internal/auth` 补单元测试

- **现状**：14 个源文件，只有 2 个 `_test.go`。
- **做什么**：从纯函数开始（token 解析、弱密钥判定、派生密钥），不要一上来测中间件。
- **验收**：`go test -mod=mod ./internal/auth/ -cover` 覆盖率上升；新增用例要能"跑红"——
  故意改坏实现时它必须失败（否则它只是装饰）。
- **预计**：2–4 小时 · 无需数据库

## 2. 给 `cmd/chiron` 与 `cmd/chiron-cli` 补启动参数测试

- **现状**：共 14 个源文件，**零测试**。
- **做什么**：把 flag 解析与配置合并抽成可测函数（如果现在内联在 `main()` 里），
  先覆盖"缺必填项时拒绝启动"这条最贵的路径。
- **验收**：`go test -mod=mod ./cmd/...` 有输出；至少一条用例断言"缺 `APP_SECRET` 时拒绝启动"。
- **预计**：3–5 小时

## 3. 清理 `prometheus_alerts.yml` 里的死规则

- **现状**：`auth_failures_total` 与 `billing_event_dropped_total` 在**代码里没有任何写入点**
  （全仓搜索只在告警文件里出现）—— 这两条规则永远不会触发。
- **做什么**：二选一。删掉这两条规则（1 小时）；或补上写入点（较大，建议先开 issue 讨论口径）。
- **验收**：`grep -rn "auth_failures_total" --include=*.go --include=*.py .` 与告警文件里的引用
  要么同时存在、要么同时消失。
- **预计**：1 小时（选删除）

## 4. 理顺 `CONTRIBUTING.md` 与 `docs/contributing.md` 的分工

- **现状**：根目录的 `CONTRIBUTING.md` 面向"怎么参与"，`docs/contributing.md` 是旧的
  "本地命令 ↔ CI job 对照"，两边有重复的提交前清单。
- **做什么**：确定分工（根目录=怎么参与；docs=CI 对照与验收口径），删重复段，两边互相链接。
- **验收**：`python scripts/check_doc_links.py` 绿；两页不再各有一份"提交前命令"清单。
- **预计**：1–2 小时

## 5. 给 `docs/wiki/` 加一个英文 landing page

- **现状**：wiki 全中文，README 只有一段英文摘要。
- **做什么**：新增 `docs/wiki/README.en.md`（架构一段 + 5 分钟跑起来 + 链到中文各页），
  并在 `docs/wiki/README.md` 顶部互链。
- **验收**：`python scripts/check_doc_links.py` 绿（相对链接会被真实校验）。
- **预计**：2 小时 · 需要英文写作

## 6. Windows 下的等价开发命令

- **现状**：`Makefile` 依赖 `make` / `test -f` / `rm -rf`，而 Windows 默认没有 `make`。
- **做什么**：提供 `scripts/dev.ps1`（至少 `check` 与 `dev-up` 两个子命令），在 CONTRIBUTING 里注明。
- **验收**：Windows 上 `powershell -File scripts/dev.ps1 check` 能跑通 Go build/vet + 4 个文档守卫。
- **预计**：2–3 小时 · 需要 Windows 环境

## 7. 统一 `scripts/check_*.py` 的帮助与退出码说明

- **现状**：几个守卫脚本的 `--list` / `--write-baseline` / 退出码语义不完全一致。
- **做什么**：统一 `--help` 文案（它拦什么、基线在哪、什么时候才该用 `--write-baseline`），
  并在 `CONTRIBUTING.md` 的表格里补一列"退出码含义"。
- **验收**：逐个 `--help` 都能说清那三件事。
- **预计**：2 小时

---

## 给维护者：怎么把它们变成 issue

- **标题**直接用上面的 `## N.` 行；
- **正文**贴该条的"现状 / 做什么 / 验收"三段，别写成一整段——新人需要知道**什么时候算做完**；
- **标签**：`good first issue` + `area/*`（gateway / engine / frontend / docs / ops）；
- 在 issue 末尾补一句"不确定就开 draft PR 问，不用憋到最后"。
