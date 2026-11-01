'use strict'
/**
 * 回归测试：切到云端模式之后，服务地址改不了。
 *
 * 现场（这台机器的 `%APPDATA%/frontend-vue/config.json`，用户手动验证时留下的）：
 *
 *   { "mode": "cloud", "apiUrl": "http://localhost:8080" }   ← 上个时代留下的地址，本机没人听
 *
 * 旧判据是"地址非空就进主界面"，于是三条路同时堵死：
 *   1. 配置页 `setup.html` 只在地址**为空**时出现 —— 陈旧地址永远非空，它永不出现；
 *   2. 设置页里根本没有服务地址这一项，而设置页属于登录之后的界面，
 *      后端不可达就登不了录，进不去；
 *   3. 唯一的入口是菜单里那个「连接设置…」（`Ctrl+,`），而窗口是 `autoHideMenuBar`
 *      —— 菜单栏平时看不见，等于没有入口。
 * 用户看到的就是"界面起来了、到处报请求失败、地址无处可改"，只能手删 config.json。
 *
 * 这里锁两件事：**切进云端必然先过地址确认**（分流 + 落盘标记 + 切换打标），
 * 以及**确认页不许再把"保存"锁在"测通"之后**（服务还没起来 / 端口写错的时候，
 * 恰恰最需要能把地址存下来）。
 */

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const config = require('../config.cjs')
const { resolveStartupWindow } = require('../shared.cjs')
const { switchMode } = require('../mode-switch.cjs')

/** 每个用例一个独立 userData 目录，绝不碰真实的 %APPDATA% */
function useTempUserData(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-api-url-setup-'))
  const previous = process.env.CHIRON_DESKTOP_USER_DATA
  process.env.CHIRON_DESKTOP_USER_DATA = dir
  t.after(() => {
    if (previous === undefined) delete process.env.CHIRON_DESKTOP_USER_DATA
    else process.env.CHIRON_DESKTOP_USER_DATA = previous
    fs.rmSync(dir, { recursive: true, force: true })
  })
  return dir
}

function readSetupHtml() {
  return fs.readFileSync(path.join(__dirname, '..', 'setup.html'), 'utf8')
}

const mainSource = fs
  .readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')
  .replace(/\r\n/g, '\n')

/** 取某个顶层函数的源码体（从声明处到下一个顶格 `}`）；源文件是 CRLF，先归一化 */
function functionBody(source, declaration) {
  const start = source.indexOf(declaration)
  assert.ok(start > 0, `源码里找不到 ${declaration}`)
  const end = source.indexOf('\n}\n', start)
  assert.ok(end > start, `${declaration} 之后找不到函数结束`)
  return source.slice(start, end)
}

/**
 * 取 setup.html 里缩进在 `<script>` 内的那一段（函数与监听器都以 `      }` / `      })` 收尾）。
 * `functionBody` 找的是顶格 `}`，对这种整段缩进的文件不适用。
 */
function scriptBlock(text, anchor, closer) {
  const start = text.indexOf(anchor)
  assert.ok(start > 0, `源码里找不到 ${anchor}`)
  const end = text.indexOf(closer, start)
  assert.ok(end > start, `${anchor} 之后找不到结束 ${JSON.stringify(closer)}`)
  return text.slice(start, end)
}

// ------------------------------------------------------------------ 建窗分流
//
// 这一组是缺陷的正身：`target` 只由"地址非空"决定时，陈旧地址就足以绕过配置页。

test('分流：云端 + 陈旧地址 + 待确认标记 ⇒ 配置页（本轮修掉的就是这条）', () => {
  const route = resolveStartupWindow({
    mode: 'cloud',
    apiUrl: 'http://localhost:8080',
    pendingApiUrl: true,
  })
  assert.equal(route.target, 'setup', '刚切进云端时必须先确认地址，不能带旧地址进主界面')
  assert.equal(route.apiUrl, 'http://localhost:8080', '配置页要预填旧地址，看不见就谈不上改')
})

test('分流：云端 + 已有地址 + 没有标记 ⇒ 直接进主界面（正常重启不许被拦）', () => {
  const route = resolveStartupWindow({ mode: 'cloud', apiUrl: 'https://chiron.example.com' })
  assert.equal(route.target, 'app')
})

test('分流：云端 + 地址为空 ⇒ 配置页（原有行为，不许被这次改动弄丢）', () => {
  assert.equal(resolveStartupWindow({ mode: 'cloud', apiUrl: '' }).target, 'setup')
})

test('分流：菜单「连接设置…」强制进配置页，即使地址看着没问题', () => {
  const route = resolveStartupWindow({ mode: 'cloud', apiUrl: 'https://a.b', forceSetup: true })
  assert.equal(route.target, 'setup')
})

test('分流：本地 + 后端就绪 ⇒ 主界面，注入的是本机 baseUrl', () => {
  const route = resolveStartupWindow({
    mode: 'local',
    apiUrl: '',
    localBackend: { ok: true, baseUrl: 'http://127.0.0.1:45126' },
  })
  assert.equal(route.target, 'app')
  assert.equal(route.apiUrl, 'http://127.0.0.1:45126')
})

test('分流：本地 + 后端没起来 ⇒ 错误页，且不把云地址带进本地分区', () => {
  const route = resolveStartupWindow({
    mode: 'local',
    apiUrl: 'https://chiron.example.com',
    localBackend: { ok: false, baseUrl: '' },
  })
  assert.equal(route.target, 'error')
  assert.equal(route.apiUrl, '')
})

test('★ 分流：待确认标记只属于云端语义，绝不能把本地模式送去配置页', () => {
  // 盘上可能留着一个没消费完的标记（切到云端又立刻切回本地）。本地模式建窗时
  // 根本不读它 —— 否则本地用户会被一张"连接云服务"的卡片挡住。
  const route = resolveStartupWindow({
    mode: 'local',
    apiUrl: '',
    localBackend: { ok: true, baseUrl: 'http://127.0.0.1:1' },
    pendingApiUrl: true,
    forceSetup: true,
  })
  assert.equal(route.target, 'app')
})

// ------------------------------------------------------------------ 落盘标记

test('标记：置位可读，消费一次即清', t => {
  useTempUserData(t)
  assert.equal(config.readDeviceSettings().setup.pendingApiUrl, false)

  config.markApiUrlPending()
  assert.equal(config.readDeviceSettings().setup.pendingApiUrl, true)

  assert.equal(config.consumeApiUrlPending(), true, '第一次消费要报"确实有待确认"')
  assert.equal(config.readDeviceSettings().setup.pendingApiUrl, false)
  assert.equal(config.consumeApiUrlPending(), false, '消费过就不该再拦下一次启动')
})

test('标记：没有标记时消费不写盘（正常启动不许反复重写 config.json）', t => {
  const dir = useTempUserData(t)
  config.saveMode('local')
  const file = path.join(dir, 'config.json')
  const original = fs.writeFileSync
  const writes = []
  fs.writeFileSync = function (target, ...rest) {
    if (String(target) === file) writes.push(String(target))
    return original.call(fs, target, ...rest)
  }
  t.after(() => {
    fs.writeFileSync = original
  })

  assert.equal(config.consumeApiUrlPending(), false)
  assert.deepEqual(writes, [], '空标记也要写盘的话，每次启动都会碰一次配置文件')

  config.markApiUrlPending()
  assert.equal(config.consumeApiUrlPending(), true)
  assert.equal(writes.length, 2, '置位 + 消费各写一次：真的有事才写')
})

test('标记：setup 段被手改成畸形形状时收敛成 false，而不是抛错挡启动', t => {
  const dir = useTempUserData(t)
  for (const junk of ['"yes"', 'null', '42', '[]']) {
    fs.writeFileSync(
      path.join(dir, 'config.json'),
      `{"mode":"cloud","apiUrl":"https://a.b","setup":${junk}}`,
      'utf8',
    )
    assert.equal(config.readDeviceSettings().setup.pendingApiUrl, false, `畸形值 ${junk} 不该被当成真`)
  }
})

test('标记：与 apiUrl 互不干扰（切模式保留地址这条契约不能被标记带跑）', t => {
  useTempUserData(t)
  config.saveApiUrl('http://127.0.0.1:8080')
  config.markApiUrlPending()
  config.saveMode('cloud')
  const settings = config.readDeviceSettings()
  assert.equal(settings.apiUrl, 'http://127.0.0.1:8080', '契约 §1：切模式不丢云端地址')
  assert.equal(settings.setup.pendingApiUrl, true, 'saveMode 不该顺手把待确认标记吃掉')
})

// ------------------------------------------------------------------ 切换打标

function fakeElectronApp() {
  return {
    app: { getPath: () => '', relaunch: () => {}, exit: () => {} },
    session: {
      fromPartition: () => ({
        clearStorageData: async () => {},
        clearCache: async () => {},
        clearAuthCache: async () => {},
      }),
    },
  }
}

function runSwitch(nextMode) {
  return switchMode({
    nextMode,
    electron: fakeElectronApp(),
    tokenStore: { clearToken: async () => true },
    restart: false,
    log: () => {},
  })
}

test('切换：切进云端 ⇒ 落盘待确认标记，返回值里也看得见', async t => {
  useTempUserData(t)
  config.saveMode('local')
  config.saveApiUrl('http://localhost:8080')

  const result = await runSwitch('cloud')

  assert.equal(result.needsApiUrl, true)
  assert.equal(config.readDeviceSettings().setup.pendingApiUrl, true)
  assert.equal(config.readDeviceSettings().apiUrl, 'http://localhost:8080')
})

test('切换：切回本地 ⇒ 清掉标记，不给下次留一个来路不明的"待确认"', async t => {
  useTempUserData(t)
  config.saveMode('cloud')
  config.markApiUrlPending()

  const result = await runSwitch('local')

  assert.equal(result.needsApiUrl, false)
  assert.equal(config.readDeviceSettings().setup.pendingApiUrl, false)
})

test('切换：同模式重复切换是幂等早退，不把标记重新点亮', async t => {
  useTempUserData(t)
  config.saveMode('cloud')
  config.consumeApiUrlPending() // 已经确认过了

  const result = await runSwitch('cloud')

  assert.equal(result.skipped, true)
  assert.equal(config.readDeviceSettings().setup.pendingApiUrl, false, '幂等路径不许再要求确认一次')
})

// ------------------------------------------------------------------ 配置页本身
//
// setup.html 是静态页、跑在 Electron 渲染进程里，行为只能端到端验；这里用源码断言
// 钉住"这一轮刚刚改掉的那条硬闸门"，防止下次被"更安全"的写法顺手改回去。

test('配置页：保存只卡地址格式，不再卡"必须先测通"', () => {
  const body = scriptBlock(readSetupHtml(), 'function sync() {', '\n      }')
  assert.match(body, /saveBtn\.disabled = pending \|\| !isStorableUrl\(value\)/)
  assert.doesNotMatch(
    body,
    /saveBtn\.disabled[^;]*!verified/,
    '又用"测没测通"挡保存：服务还没起、端口写错、探测被防火墙挡住的时刻，恰恰最需要把地址存下来',
  )
})

test('配置页：打开时预填当前地址', () => {
  const html = readSetupHtml()
  assert.match(html, /await host\.getDeviceSettings\(\)/, '不读设备设置就不知道旧地址是什么')
  assert.match(html, /urlInput\.value = settings\.apiUrl/)
})

test('配置页：有回本地模式的出口（它常常是当时唯一的窗口）', () => {
  const html = readSetupHtml()
  assert.match(html, /<button id="local"/)
  assert.match(html, /await host\.setMode\('local'/)
})

test('配置页：仍然不可能"测了 A 却保存 B"—— 保存的就是输入框里那个值', () => {
  const save = scriptBlock(
    readSetupHtml(),
    "saveBtn.addEventListener('click', async () => {",
    '\n      })',
  )
  assert.match(save, /const value = urlInput\.value\.trim\(\)/)
  assert.match(save, /await host\.setApiUrl\(value\)/)
  assert.doesNotMatch(save, /setApiUrl\(verified\)/, '测通一次就锁死旧地址，改输入框等于没改')
})

// ------------------------------------------------------------------ 主进程接线

test('main.cjs：建窗走分流纯函数，并在云端消费待确认标记', () => {
  const body = functionBody(mainSource, 'function createWindow()')
  assert.match(body, /resolveStartupWindow\(/)
  assert.match(body, /consumeApiUrlPending\(\)/)
  assert.doesNotMatch(
    body,
    /target = !forceSetup && apiUrl/,
    '又退回"地址非空就算配好"：陈旧地址会再次绕过配置页',
  )
})

test('main.cjs：菜单之外还要有 IPC 入口，设置页与登录页才够得着配置页', () => {
  assert.match(
    mainSource,
    /ipcMain\.handle\('desktop:open-setup', event => \{\s*assertTrustedSender\(event\)\s*relaunchIntoSetup\(\)/,
    '改地址是敏感动作：必须校验 sender，且落到重建配置页那一条',
  )
  const preload = fs.readFileSync(path.join(__dirname, '..', 'preload.cjs'), 'utf8')
  assert.match(preload, /openSetup: \(\) => ipcRenderer\.invoke\('desktop:open-setup'\)/)
})

test('main.cjs：首启就选云端时同样先确认地址（老配置的 apiUrl 未必还连得上）', () => {
  const body = functionBody(mainSource, 'async function askInitialMode(settings)')
  assert.match(body, /if \(mode === 'cloud'\) markApiUrlPending\(\)/)
})
