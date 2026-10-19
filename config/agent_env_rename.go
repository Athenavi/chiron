package config

import (
	"fmt"
	"os"
	"sort"
	"strings"
)

// ── M20：Agent 预算变量的**破坏性改名**与启动拦截 ──────────────────────────
//
// 起因是"同名不同义"：`AGENT_MAX_TOKENS` 在网关里是"单次调用的 max output tokens"
// （默认 8192），在**引擎**里却是"一个 run 全程的 token 预算"（默认 400k）。
// 共享 env 的部署按网关口径设 8192 ⇒ 引擎几乎每个 run 都以 `budget_exceeded:tokens`
// 收口，而两侧都没有任何校验。`AGENT_MAX_TURNS`、`AGENT_CONTEXT_LIMIT` 同族。
//
// 处置（用户拍板）：**直接破坏性改名**，不做双读兼容 —— 因为"旧名继续能读"恰恰会让
// 那个 8192 继续生效，把最危险的形态留在线上。代价是旧配置会静默失效，所以必须有这道
// **显式**拦截：启动时发现旧名被设置为非空值就拒绝启动，并直接给出该换成哪个新名。
//
// 判据刻意简单：只看"变量是否被设置为非空字符串"，不看它是否能解析成数字 ——
// 一个写错的旧名（如 `AGENT_MAX_TOKENS=abc`）同样要拦，否则部署者以为它生效了。

// renamedAgentEnv：旧名 → 迁移提示（该换成哪个新名、为什么）。
var renamedAgentEnv = map[string]string{
	"AGENT_MAX_TOKENS": "AGENT_MAX_OUTPUT_TOKENS（网关：**单次调用**的 max output tokens）" +
		" 或 AGENT_RUN_MAX_TOKENS（引擎：一个 run **全程**的 token 预算）",
	"AGENT_MAX_TURNS": "AGENT_DEFAULT_MAX_TURNS（网关：每次提交的默认回合数）" +
		" 或 AGENT_RUN_MAX_TURNS（引擎：一个 run 的回合上限）",
	"AGENT_CONTEXT_LIMIT": "（该开关已删除：引擎按模型窗口的 **token** 预算压缩，没有" +
		"『消息条数上限』这个量；后台设置页的对应输入框也已摘掉）",
}

// ValidateRenamedAgentEnv 检查已废弃的 Agent 预算变量是否仍被设置。
//
// 返回 nil = 干净；否则返回的 error 已经写清"哪个变量、当前值、该换成什么"，
// 调用方（`cmd/chiron/main.go`）应当**拒绝启动**并原样打印 —— 这是本次改名的
// 唯一护栏：宁可启动失败，也不要让一个"看起来设了、其实不被任何人读"的值留在线上。
func ValidateRenamedAgentEnv() error {
	var stale []string
	for old, hint := range renamedAgentEnv {
		value, ok := os.LookupEnv(old)
		if !ok || strings.TrimSpace(value) == "" {
			continue
		}
		stale = append(stale, fmt.Sprintf("%s=%s ⇒ 请改用 %s", old, strings.TrimSpace(value), hint))
	}
	if len(stale) == 0 {
		return nil
	}
	sort.Strings(stale)
	return fmt.Errorf(
		"检测到已废弃的环境变量（M20 破坏性改名，旧名不再被任何一侧读取）：\n  - %s\n"+
			"改名原因：AGENT_MAX_TOKENS / AGENT_MAX_TURNS 在网关与引擎里**含义不同**，"+
			"同名会让一侧的配置静默顶掉另一侧；现在单次/默认一类的名字归网关，run 级的名字带 RUN_ 归引擎",
		strings.Join(stale, "\n  - "))
}
