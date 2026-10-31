#!/usr/bin/env node
/**
 * 打包前把 Go 网关建到 `frontend-vue/assets/gateway/` —— 安装包里 `resources/chiron(.exe)` 的唯一产出点。
 *
 * **为什么要单独一个脚本，而不是在 CI 里顺手写一行 `go build`：**
 * `electron-builder.yml` 的 extraResources 用 **glob 过滤**取 `chiron`/`chiron.exe`，
 * 而 app-builder-lib 对匹配不到的 glob **只 warn 不报错**（`out/fileMatcher.js`：
 * `statOrNull(from)` 为 null 就 `log.warn` 后 `return`）。于是"忘了建网关"不会让打包失败，
 * 只会打出一个**没有业务入口的残包**：桌面壳退回 `desktop-local` core，而 core 只实现
 * `/v1/**` 的一小部分（见 docs/desktop-local/endpoint-coverage.md），用户看到的就是
 * "本地模式什么都用不了"。这正是本脚本要把静默降级换成非零退出的原因。
 *
 * 因为 CI 与 `pnpm desktop:pack|dist` 走同一份脚本，本地出包与 CI 出包不会各说一套。
 *
 * **为什么产物落 `assets/gateway/` 而不是仓库根：**
 * 仓库根的 `chiron(.exe)` 是**开发态产物**，由 `run.py` 在源码比产物新时自动重建，
 * `main.cjs::resolveGatewayExe()` 也只在开发态去那里找。把它同时当成打包输入有两个坑：
 *   1. 出包会覆盖（或因网关正在运行而写不进）开发者刚建的二进制；
 *   2. 更要紧的是**陈旧**：根下那份可能是上个提交建的，跳过构建步骤照样能被 glob 命中，
 *      于是安装包里是个"能跑但与前端不是同一份源码"的网关 —— 静默失效换了个形态而已。
 * `assets/gateway/` 只由这一步产出，与 uv / CPython 的随包资产同一套约定
 * （`assets/uv/`、`assets/python-runtime/`，都在 `.gitignore` 里整目录忽略）。
 *
 * 约定：
 *   - 目录名必须与 `electron-builder.yml` 的 `from:` 一字不差，用例把这条耦合钉住；
 *   - `GOOS`/`GOARCH` 显式给了就用（CI 按 electron-builder 的 target 架构传，
 *     而不是按 runner 架构 —— `macos-latest` 是 arm64 runner，mac target 却是 x64）；
 *   - `CGO_ENABLED=0` 是默认而非建议：与 Makefile / Dockerfile 同一口径，
 *     交叉编译时带 CGO 会链接失败，本机编译则会产出依赖 msvcrt 的二进制。
 */

import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/** `go build` 的包路径（相对仓库根）。 */
export const GATEWAY_PACKAGE = './cmd/chiron'

/**
 * 随包网关的落点，**相对 frontend-vue/**（electron-builder 的 `from` 以配置文件所在目录为基准）。
 * 改名必须与 `electron-builder.yml` 的 `from:` 同步 —— build-gateway.test.cjs 会拦。
 */
export const GATEWAY_OUTPUT_DIR = 'assets/gateway'

/** runner/开发机平台名 → Go 的 GOOS。 */
export const GOOS_BY_PLATFORM = {
  win32: 'windows',
  darwin: 'darwin',
  linux: 'linux',
  freebsd: 'freebsd',
}

/** 网关可执行文件名：Windows 必须带 `.exe`，否则 `spawn` 直接 ENOENT。 */
export function gatewayArtifactName(platform = process.platform) {
  return platform === 'win32' ? 'chiron.exe' : 'chiron'
}

/** `assets/gateway` 相对仓库根的绝对目录（与 frontend-vue/ 同级的那个 frontend-vue/assets）。 */
export function gatewayOutputDir(root = defaultRepoRoot()) {
  return path.join(root, 'frontend-vue', ...GATEWAY_OUTPUT_DIR.split('/'))
}

/**
 * 组装一次网关构建（纯函数，不碰文件系统）—— 拆开是为了让"参数怎么拼"这件事可被用例钉住。
 *
 * @param {object} [input]
 * @param {string} [input.platform] 宿主平台（决定默认 GOOS 与产物名）
 * @param {string} [input.root]     仓库根
 * @param {string} [input.goos]     显式 GOOS（交叉编译）
 * @param {string} [input.goarch]   显式 GOARCH
 * @param {string} [input.goBin]    go 可执行文件（默认 `go`，走 PATH）
 */
export function planGatewayBuild(input = {}) {
  const platform = input.platform || process.platform
  const root = input.root || defaultRepoRoot()
  const goos = input.goos || GOOS_BY_PLATFORM[platform] || platform
  const goarch = input.goarch || 'amd64'
  const dir = gatewayOutputDir(root)
  const output = path.join(dir, gatewayArtifactName(platform))
  return {
    goBin: input.goBin || 'go',
    cwd: root,
    dir,
    output,
    goos,
    goarch,
    // `-mod=mod` 是必需的：仓库里的 vendor/ 放的是参考源码（不含 vendor/modules.txt），
    // Go 只要看到 vendor/ 就会按 vendor 模式做一致性检查并报 "inconsistent vendoring"
    // （与 ci.yml / Makefile / Dockerfile 同一口径）。
    args: ['build', '-mod=mod', '-ldflags', '-s -w', '-o', output, GATEWAY_PACKAGE],
    env: { CGO_ENABLED: '0' },
  }
}

/** 仓库根：本文件在 `<root>/frontend-vue/scripts/` 下。 */
export function defaultRepoRoot() {
  const here = path.dirname(fileURLToPath(import.meta.url))
  return path.resolve(here, '..', '..')
}

/**
 * 产物体检。electron-builder 的 glob 只看文件在不在，**不看它能不能跑**，
 * 所以这里至少把"不存在"和"零字节"这两种残包挡下来。
 */
export function inspectArtifact(output) {
  let stat
  try {
    stat = fs.statSync(output)
  } catch {
    return { ok: false, reason: `产物不存在：${output}` }
  }
  if (!stat.isFile()) return { ok: false, reason: `产物不是普通文件：${output}` }
  if (stat.size === 0) return { ok: false, reason: `产物是空文件：${output}` }
  return { ok: true, size: stat.size }
}

/**
 * 执行一次构建。失败一律抛错 —— 调用方（npm script / CI）非零退出，
 * 不允许"没网关也照样出包"。
 */
export function buildGateway(input = {}) {
  const plan = planGatewayBuild(input)
  // `go build -o` 不会创建目录，而 assets/gateway 是整目录忽略、不入库的产物目录，
  // CI 的干净检出上必然不存在 —— 必须在 spawn 之前建好，否则第一次出包必挂在这句上面。
  fs.mkdirSync(plan.dir, { recursive: true })
  const result = spawnSync(plan.goBin, plan.args, {
    cwd: plan.cwd,
    stdio: 'inherit',
    env: { ...process.env, GOOS: plan.goos, GOARCH: plan.goarch, ...plan.env, ...(input.env || {}) },
  })

  if (result.error) {
    const hint =
      result.error.code === 'ENOENT'
        ? `找不到 \`${plan.goBin}\`。装 Go（${'https://go.dev/dl/'}），或用 GO_BIN 指到具体可执行文件；` +
          '不能跳过这一步 —— 跳过等于打一个没有业务入口的安装包。'
        : `调用 ${plan.goBin} 失败：${result.error.message}`
    throw new Error(hint)
  }
  if (result.status !== 0) {
    throw new Error(`go build 退出码 ${result.status}（GOOS=${plan.goos} GOARCH=${plan.goarch}）`)
  }

  const check = inspectArtifact(plan.output)
  if (!check.ok) throw new Error(check.reason)
  return { ...plan, size: check.size }
}

function main() {
  const goos = String(process.env.GOOS || '').trim()
  const goarch = String(process.env.GOARCH || '').trim()
  const built = buildGateway({
    goos: goos || undefined,
    goarch: goarch || undefined,
    goBin: String(process.env.GO_BIN || '').trim() || undefined,
  })
  const tag = goos ? `${built.goos}-${built.goarch}` : 'host'
  process.stderr.write(
    `网关已就位：${built.output}（${tag}，${(built.size / 1024 / 1024).toFixed(1)} MB）\n`,
  )
}

// 只有被直接执行时才跑（被 import 时只暴露纯函数，供测试用）。
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main()
  } catch (err) {
    process.stderr.write(`::error::${err.message}\n`)
    process.exitCode = 1
  }
}
