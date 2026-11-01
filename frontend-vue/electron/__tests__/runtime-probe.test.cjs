'use strict'
/**
 * 运行时导入探测的环境契约（`electron/runtime-probe.cjs`）。
 *
 * 真机验证本地模式时踩到的两类"探测必然失败"：
 *   1. 引擎里约 60 个模块在 **import 期**构造 `Settings`（`app/config.py` 校验
 *      `JWT_SECRET >= 32`），探测只继承 `process.env` 时这些模块全部抛
 *      `ValidationError` —— 与依赖装没装齐无关，打包态的引擎环境永远判不可用；
 *   2. 两处调用（安装验收、启动前选解释器）如果各自的探测条件不同，就会出现
 *      "验收说装好了、启动时又说不行"，而这正是这份文件存在的理由。
 *
 * 全部用注入的假 spawn，不真的起解释器。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { RUNTIME_PROBE_CODE, PROBE_ENV, probeDataEnv, extractMissingModule, probeRuntime } = require('../runtime-probe.cjs')

/** 记录调用参数的假 spawnSync */
function recordingSpawn(status = 0) {
  const calls = []
  const spawnSync = (cmd, args, opts) => {
    calls.push({ cmd, args, opts })
    return { status, stdout: '', stderr: '' }
  }
  return { spawnSync, calls }
}

test('探测自带可导入所需的最小 env，不看本机配了什么密钥', () => {
  const { spawnSync, calls } = recordingSpawn()
  const result = probeRuntime(spawnSync, 'C:/venv/Scripts/python.exe', 'C:/app/engine')

  assert.equal(result.ok, true)
  const env = calls[0].opts.env
  for (const [key, value] of Object.entries(PROBE_ENV)) {
    assert.equal(env[key], value, `${key} 必须由探测自己给出：引擎的 Settings 在 import 期就要它`)
    assert.ok(
      String(value).length >= 32,
      `${key} 的占位值得过 >=32 的强度校验，短了等于没给`,
    )
  }
  assert.equal(env.PYTHONNOUSERSITE, '1', '不许用户级 site-packages 混进探测')
})

test('探测结果不受本机残缺密钥影响', () => {
  // 用户机器上可能有一个太短的 APP_SECRET（配置页填了一半）。
  // 若探测照抄它，引擎探测就会因为密钥强度失败，用户看到的却是"依赖没装好"。
  const previous = { APP_SECRET: process.env.APP_SECRET, JWT_SECRET: process.env.JWT_SECRET }
  process.env.APP_SECRET = 'short'
  process.env.JWT_SECRET = 'short'
  try {
    const { spawnSync, calls } = recordingSpawn()
    probeRuntime(spawnSync, 'C:/venv/Scripts/python.exe', 'C:/app/engine')
    assert.equal(calls[0].opts.env.APP_SECRET, PROBE_ENV.APP_SECRET)
    assert.equal(calls[0].opts.env.JWT_SECRET, PROBE_ENV.JWT_SECRET)
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  }
})

test('探测自带可写的落盘路径：cwd 只读不该被判成"依赖没装好"', () => {
  // 真机事故形状：cwd 是 `Program Files\Chiron\resources\python-engine`（只读），
  // 而引擎有若干模块按**相对 cwd** 的默认路径 mkdir（`app/skill/store.py:296`）。
  // 于是 import 变成一次写盘，探测抛 PermissionError 被判"依赖没装好"，
  // 用户被指向 requirements.txt —— 与事实无关。
  const { spawnSync, calls } = recordingSpawn()
  probeRuntime(spawnSync, 'C:/venv/Scripts/python.exe', 'C:/Program Files/Chiron/resources/python-engine')

  const env = calls[0].opts.env
  for (const [key, value] of Object.entries(probeDataEnv(os.tmpdir()))) {
    assert.equal(env[key], value, `${key} 必须由探测自己给出，且是绝对路径`)
    assert.ok(path.isAbsolute(value), `${key} 不是绝对路径：${value}`)
  }
})

test('调用方显式传入的 env 仍然优先', () => {
  const { spawnSync, calls } = recordingSpawn()
  probeRuntime(spawnSync, 'C:/venv/Scripts/python.exe', 'C:/app/engine', {
    env: { APP_SECRET: 'real-secret-for-this-probe-0123456789' },
  })
  assert.equal(calls[0].opts.env.APP_SECRET, 'real-secret-for-this-probe-0123456789')
})

test('探测必须收集 walk_packages 的错误，否则子树失败会被静默吞掉', () => {
  // pkgutil.walk_packages 的 onerror 默认是 None：某个子包 import 失败时它既不报告、
  // 也不再遍历它的子孙，探测于是"通过"。这条是源码级守卫，改探测代码时不许丢。
  assert.match(RUNTIME_PROBE_CODE, /onerror=_onerror/)
  assert.match(RUNTIME_PROBE_CODE, /pkgutil\.walk_packages/)
  assert.match(RUNTIME_PROBE_CODE, /sys\.exit\(1 if missing else 0\)/)
})

test('认不出缺失模块时不猜：只说"无法导入"', () => {
  const missing = extractMissingModule("MISS app.foo: ModuleNotFoundError: No module named 'httpx'")
  assert.equal(missing, 'httpx')
  assert.equal(extractMissingModule('MISS app.foo: ValidationError: JWT_SECRET must be set'), '')

  assert.equal(extractMissingModule('MISS app.foo: ValidationError: JWT_SECRET must be set'), '')

  const result = probeRuntime(
    () => ({ status: 1, stdout: 'MISS app.foo: ValidationError: boom', stderr: '' }),
    'C:/venv/Scripts/python.exe',
    'C:/app/engine',
  )
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'probe-failed', '认不出的失败不该被说成"缺依赖"')
})

test('两处调用共用同一份探测实现', () => {
  // 安装验收与启动前选解释器必须问同一个问题、用同样的条件。
  const dir = path.join(__dirname, '..')
  for (const file of ['components.cjs', 'runtime-install.cjs']) {
    const source = fs.readFileSync(path.join(dir, file), 'utf8')
    assert.match(source, /require\('\.\/runtime-probe\.cjs'\)/, `${file} 应复用 runtime-probe`)
    assert.ok(
      !source.includes('walk_packages'),
      `${file} 里不该出现第二份遍历实现 —— 那会造出"验收绿、启动红"`,
    )
  }
})
