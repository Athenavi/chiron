'use strict'
/**
 * `scripts/build-gateway.mjs` 的契约测试。
 *
 * 这个脚本守的是一件**静默失效**：electron-builder 对匹配不到的 extraResources glob
 * 只 warn 不报错（`out/fileMatcher.js`），所以"打包机上没建网关"照样出包 —— 装上之后
 * 本地模式退回 core、几乎所有页面空转，而 CI 是全绿的。用例分三簇：
 *
 * 1. **产物名与落点**：`main.cjs::resolveGatewayExe()` 只认 `resources/` 根的 `chiron`/`chiron.exe`，
 *    extraResources 从 `assets/gateway/` glob —— 脚本、yml、主进程任意一侧改名，
 *    包里就没有网关且无人报错。用例把"脚本写的目录"与"yml 读的目录"锁成同一个导出常量。
 * 2. **构建参数**：`-mod=mod`（仓库里的 `vendor/` 是参考源码，缺这个参数 Go 直接
 *    报 inconsistent vendoring）、`CGO_ENABLED=0`、`-ldflags -s -w`；
 * 3. **失败必须抛**：ENOENT 也要抛 —— 这个脚本的全部意义就是"不许跳过"。
 */

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

/** ESM 模块只能在 async 用例里动态 import。 */
async function loadScript() {
  return await import('../../scripts/build-gateway.mjs')
}

test('产物名与 resolveGatewayExe() 一致：win32 带 .exe，其余不带', async () => {
  const { gatewayArtifactName } = await loadScript()
  assert.equal(gatewayArtifactName('win32'), 'chiron.exe')
  assert.equal(gatewayArtifactName('darwin'), 'chiron')
  assert.equal(gatewayArtifactName('linux'), 'chiron')
})

test('产物落 frontend-vue/assets/gateway/，不落仓库根（根下那份是 run.py 的开发态产物）', async () => {
  const { planGatewayBuild, defaultRepoRoot, GATEWAY_OUTPUT_DIR } = await loadScript()
  const root = path.join('X:', 'repo')
  const plan = planGatewayBuild({ platform: 'win32', root })
  assert.equal(plan.output, path.join(root, 'frontend-vue', 'assets', 'gateway', 'chiron.exe'))
  assert.equal(plan.dir, path.dirname(plan.output))
  // cwd 仍是仓库根：go.mod 在那儿。
  assert.equal(plan.cwd, root)
  // 绝不能退回仓库根 —— 那会覆盖开发者的二进制，且跳过构建时陈旧产物照样被 glob 命中。
  assert.notEqual(plan.output, path.join(root, 'chiron.exe'))

  // 默认仓库根 = 本文件的 ../..（frontend-vue/scripts → 仓库根）。
  // 判据用"根下必须有 frontend-vue/package.json"，不写死检出目录的名字（CI 的目录名随仓库名变）。
  const fallback = planGatewayBuild({ platform: process.platform })
  assert.equal(fallback.cwd, defaultRepoRoot())
  assert.ok(
    fs.existsSync(path.join(defaultRepoRoot(), 'frontend-vue', 'package.json')),
    `仓库根解析错了：${defaultRepoRoot()}`,
  )
  assert.ok(fs.existsSync(path.join(defaultRepoRoot(), 'cmd', 'chiron', 'main.go')))
  assert.ok(GATEWAY_OUTPUT_DIR.startsWith('assets/'), '随包资产目录归在 assets/ 下，与 uv / python-runtime 同一约定')
})

test('脚本的目录与 electron-builder.yml 的 from 同源：一侧改名另一侧必须跟着改', async () => {
  const { defaultRepoRoot, GATEWAY_OUTPUT_DIR } = await loadScript()
  const ymlPath = path.join(defaultRepoRoot(), 'frontend-vue', 'electron-builder.yml')
  const lines = fs.readFileSync(ymlPath, 'utf8').split(/\r?\n/)

  // `- from: assets/gateway`（相对 yml 所在目录，即 frontend-vue/）。
  const i = lines.findIndex((l) => l.trim() === `- from: ${GATEWAY_OUTPUT_DIR}`)
  assert.ok(i >= 0, `electron-builder.yml 里没有 \`from: ${GATEWAY_OUTPUT_DIR}\` —— 脚本产物不会被装进安装包`)
  // 必须映射到 resources 根：resolveGatewayExe() 打包态只在 resourcesPath 下找裸文件名。
  assert.equal(lines[i + 1].trim(), 'to: .', '网关要落在 resources/ 根，不能落进子目录')

  const filters = []
  for (let j = i + 2; j < lines.length; j += 1) {
    const t = lines[j].trim()
    if (t === 'filter:') continue
    if (!t.startsWith('- ')) break
    filters.push(t)
  }
  assert.ok(filters.some((f) => f.endsWith('"chiron.exe"')), 'filter 必须收 chiron.exe（Windows）')
  assert.ok(filters.some((f) => f.endsWith('"chiron"')), 'filter 必须收 chiron（mac/linux 裸名没有扩展名）')
})

test('构建参数：-mod=mod / ./cmd/chiron / -ldflags -s -w 一个都不能少', async () => {
  const { planGatewayBuild, GATEWAY_PACKAGE } = await loadScript()
  const plan = planGatewayBuild({ platform: 'linux', root: '/tmp/repo' })
  assert.deepEqual(plan.args.slice(0, 4), ['build', '-mod=mod', '-ldflags', '-s -w'])
  assert.equal(plan.args.at(-1), GATEWAY_PACKAGE)
  assert.equal(GATEWAY_PACKAGE, './cmd/chiron')
  // `-o <产物>` 必须成对出现，否则 go build 会在 cwd 里丢一个同名文件
  assert.equal(plan.args[plan.args.indexOf('-o') + 1], plan.output)
  assert.equal(plan.env.CGO_ENABLED, '0')
})

test('GOOS 按宿主平台映射；显式 goos/goarch 覆盖（CI 传 target 架构而非 runner 架构）', async () => {
  const { planGatewayBuild } = await loadScript()
  assert.equal(planGatewayBuild({ platform: 'darwin' }).goos, 'darwin')
  assert.equal(planGatewayBuild({ platform: 'win32' }).goos, 'windows')
  // macos-latest 是 arm64 runner，而 mac target 是 x64 ⇒ 必须能被显式覆盖
  const cross = planGatewayBuild({ platform: 'darwin', goos: 'darwin', goarch: 'amd64' })
  assert.equal(cross.goarch, 'amd64')
  assert.equal(planGatewayBuild({ platform: 'linux' }).goarch, 'amd64')
})

test('产物体检：不存在 / 零字节 / 不是普通文件都算残包', async () => {
  const { inspectArtifact } = await loadScript()
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-gw-'))
  try {
    assert.equal(inspectArtifact(path.join(dir, 'nope')).ok, false)

    const empty = path.join(dir, 'empty')
    fs.writeFileSync(empty, '')
    assert.equal(inspectArtifact(empty).ok, false)

    const good = path.join(dir, 'good')
    fs.writeFileSync(good, 'x'.repeat(10))
    assert.equal(inspectArtifact(good).ok, true)
    assert.equal(inspectArtifact(good).size, 10)

    assert.equal(inspectArtifact(dir).ok, false) // 目录不算产物
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('产物目录在 spawn 之前建好：go build -o 不会自己建目录，干净检出上必挂', async () => {
  const { buildGateway } = await loadScript()
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-gw-root-'))
  try {
    const nested = path.join(root, 'frontend-vue', 'assets', 'gateway')
    assert.equal(fs.existsSync(nested), false)
    assert.throws(() => buildGateway({ root, goBin: path.join(root, 'no-such-go') }))
    // 抛的是"找不到 go"，但目录必须已经存在 —— 顺序反了这条就红。
    assert.equal(fs.existsSync(nested), true, '没在 spawn 前 mkdir，CI 首次出包会挂在 go build 上')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('go 不可用时也抛错，而不是静默返回 —— 否则又是一次"没有网关的包"', async () => {
  const { buildGateway } = await loadScript()
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-gw-bin-'))
  try {
    assert.throws(
      () => buildGateway({ root: dir, goBin: path.join(dir, 'definitely-not-go') }),
      /不能跳过|ENOENT|失败/,
    )
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
