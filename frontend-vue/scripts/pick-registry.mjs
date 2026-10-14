#!/usr/bin/env node
/**
 * 构建期 npm registry 自动选择 —— 对候选源**测速**，取最快者。
 *
 * 背景：此前 `pnpm install` 只靠 `.npmrc` 里写死的一个 registry。受限网络下直连
 * registry.npmjs.org 会长时间挂起（甚至把构建拖到超时），而反过来在海外直连
 * npmmirror 又会慢。这里对候选源并发探测，按实测延迟选择，不再写死。
 *
 * 约定：
 *   - stdout **只**输出选中的 URL（供 `pnpm install --registry "$(node scripts/pick-registry.mjs)"`
 *     这样的命令替换使用）；一切诊断信息走 stderr；
 *   - 全部候选不可达时以非零退出码结束，由调用方兜底（Dockerfile 的 `|| echo`、
 *     CI 的 `|| echo`）到官方源 —— 此时构建本就需要该源可达；
 *   - 候选可经环境变量扩展：`NPM_REGISTRY_CANDIDATES`（逗号分隔，追加在默认候选之后），
 *     用于接入企业私有源；
 *   - 探测超时可经 `NPM_REGISTRY_PROBE_TIMEOUT_MS` 调整（默认 3000ms）。
 *
 * 依赖 Node 内置 `fetch` / `AbortSignal.timeout`（Node ≥ 17.3；本项目构建用 node:22）。
 */

const DEFAULT_CANDIDATES = [
  'https://registry.npmjs.org',
  'https://registry.npmmirror.com',
];

const TIMEOUT_MS = Number(process.env.NPM_REGISTRY_PROBE_TIMEOUT_MS || 3000);

const extra = (process.env.NPM_REGISTRY_CANDIDATES || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

// 去重：默认候选 + 环境变量追加的候选
const candidates = [...new Set([...DEFAULT_CANDIDATES, ...extra])];

const normalize = (url) => url.replace(/\/+$/, '');

async function probe(base) {
  const started = Date.now();
  try {
    // `/-/ping` 是 npm registry 的轻量健康端点，npmmirror 同样实现。
    const res = await fetch(`${normalize(base)}/-/ping`, {
      signal: AbortSignal.timeout(TIMEOUT_MS),
      headers: { 'user-agent': 'chiron-registry-probe' },
    });
    if (!res.ok) {
      console.error(`[pick-registry] ${base} => HTTP ${res.status}`);
      return null;
    }
    return { url: normalize(base), ms: Date.now() - started };
  } catch (err) {
    const reason = err && err.name ? err.name : String(err);
    console.error(`[pick-registry] ${base} => ${reason}`);
    return null;
  }
}

const results = (await Promise.all(candidates.map(probe))).filter(Boolean);
if (results.length === 0) {
  console.error(`[pick-registry] no reachable registry among: ${candidates.join(', ')}`);
  process.exit(1);
}

results.sort((a, b) => a.ms - b.ms);
for (const r of results) {
  console.error(`[pick-registry] ${r.url} => ${r.ms}ms`);
}

// stdout 仅一行：最快的源
process.stdout.write(results[0].url);
