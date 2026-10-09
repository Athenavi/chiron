# 生产使用：测试与评测

> **权威来源**：`docs/contributing.md`（CI 五个 job）· `python-engine/evals/README.md`（评测套件）·
> [transcript 契约](../../transcript-contract.md)（这块怎么测）。

## 四层测试，各管一段

| 层 | 位置 | 跑法 |
|---|---|---|
| **Go 单测** | `internal/**/*_test.go` | `go test -mod=mod ./... -count=1` |
| **Python 单测** | `python-engine/tests/` | `python -m pytest -q -m "not integration"` |
| **集成测试** | 同上，`-m integration` | 需**真实 PG + Redis + 网关** |
| **前端测试** | `frontend-vue/src/**/__tests__/` | `pnpm run test`（vitest + jsdom） |
| **评测套件** | `python-engine/evals/` | 见 `evals/README.md`（E1 / E2 / E3） |

## 集成测试要"真实栈"

`integration` 标记的用例需要真 PostgreSQL + Redis，**还要把网关也起起来**
（引擎侧统一客户端会走网关的 `/v1/internal/*`）：

```bash
redis-server --port 6390 --appendonly no --save ""
REDIS_ADDR=127.0.0.1:6390 go run ./cmd/chiron
POSTGRES_DSN=<来自 .env> REDIS_URL=redis://127.0.0.1:6390/0 \
  python -m pytest -q -m integration
```

**本机没有真实栈时不要假装跑过** —— 把没跑的项写下来（这是本仓库的一贯做法）。

## 前端怎么测"几何与滚动"

transcript 的 7 个模块是**纯几何**（不碰 DOM）⇒ 可以用**事件序列做确定性回归**，
不需要浏览器。列表容器（`MessageList`）的窗口化与锚定用 jsdom + `@vue/test-utils` 测。

**已知的边界**：**没有浏览器级 e2e 测试**。这是 UI/UX 差距分析里指出的最大系统性差距
（测试/源码字节比 0.11× vs 对标项目 0.77×）。补它不需要新依赖，缺的是**接缝**
（把内核换成可脚本化的假实现）。

## 测试自身的纪律

- **一个测试必须能观察到它声称的东西** —— 例如：`$emit` 之后**同步**读 props 是**读不到**新值的
  （Vue 会批量传播），要 `await nextTick()` 或读父组件的 setup 状态。
- **断言失败时先分清"代码错"还是"我对代码的模型错"** —— 后者很常见。
- **flaky 的第二次出现，才是"记录它"变成"修它"的临界点**（第一次应先取证）。

## 延伸阅读

- [贡献与验收](../contributing/overview.md) —— 门禁与自查清单
- [transcript](../core/transcript.md) —— 被契约化的那块怎么测
