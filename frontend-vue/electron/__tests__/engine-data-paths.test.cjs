'use strict'
/**
 * 引擎/网关的落盘路径契约（真机事故回归）。
 *
 * 现场：`%APPDATA%\frontend-vue\logs\main.log`
 *
 *   依赖装完了，但AI 引擎仍无法导入：解释器 …\engine\venv\Scripts\python.exe
 *   无法导入随包的本地代码：MISS app.tools: PermissionError: [WinError 5] 拒绝访问。: 'data'
 *   这通常意味着 requirements.txt 里少了某个包。
 *
 * 这条消息把人往"缺包"上引，但 uv 明明装完了 126 个包。真正的因果是：
 *   - python-engine 的若干模块在 **import 期**就按**相对 cwd** 的默认路径 mkdir
 *     （`app/skill/store.py:296` 的 `self._root.mkdir(...)`，root 默认 `./data/skills`）；
 *   - 打包态的 cwd 与源码都在 `Program Files\Chiron\resources\python-engine`，只读；
 *   - 于是"导入 app 包"变成一次写盘，`import app.tools` 抛 PermissionError，
 *     验收探测（`runtime-probe.cjs`）判「依赖没装好」。
 *
 * 两层都钉住：壳层必须显式下发**绝对**路径（不再依赖 cwd 可写），
 * 引擎侧导入期不许再构造 SkillStore。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const stack = require('../local-stack.cjs')
const { probeDataEnv } = require('../runtime-probe.cjs')

const VALID_SECRET = 'x'.repeat(32)
const DATA_DIR = path.join(path.sep, 'Users', 'someone', 'AppData', 'Roaming', 'gh_Athenavi_chiron', 'local')

/** 引擎最终看到的环境：`options.env` 压过 process.env，所以显式给空对象做干净基线 */
function engineEnv() {
  return stack.buildEngineEnv(
    { pgDsn: 'postgresql://x', redisAddr: '127.0.0.1:50404' },
    { appSecret: VALID_SECRET, port: 9100, dataDir: DATA_DIR, env: {} },
  )
}

function gatewayEnv() {
  return stack.buildGatewayEnv(
    { pgDsn: 'postgresql://x', redisAddr: '127.0.0.1:50404' },
    { appSecret: VALID_SECRET, port: 9200, dataDir: DATA_DIR, env: {} },
  )
}

test('buildEngineEnv 把五个相对默认值的路径顶成 <dataDir> 下的绝对路径', () => {
  const env = engineEnv()
  // 名字必须是引擎真正读的那几个；改一个就等于没下发
  for (const name of [
    'SKILL_STORE_PATH',
    'MEDIA_STORE_PATH',
    'KNOWLEDGE_LOCAL_PATH',
    'PLUGIN_DATA_DIR',
    'SANDBOX_ROOT',
  ]) {
    assert.ok(env[name], `引擎缺 ${name}：它会退回相对 cwd 的默认值，在只读目录里 mkdir 失败`)
    assert.ok(path.isAbsolute(env[name]), `${name} 必须是绝对路径，实际：${env[name]}`)
    assert.ok(
      env[name].startsWith(DATA_DIR),
      `${name} 应落在本次运行的数据目录下，实际：${env[name]}`,
    )
  }
  // 审计目录走 Settings.log_dir（env 名 LOG_DIR）
  assert.ok(env.LOG_DIR && path.isAbsolute(env.LOG_DIR), '审计目录必须给绝对路径')
  assert.ok(env.LOG_DIR.startsWith(DATA_DIR))
})

test('每个键指向各自子目录，不互相吞掉', () => {
  const env = engineEnv()
  const values = [
    env.SKILL_STORE_PATH,
    env.MEDIA_STORE_PATH,
    env.KNOWLEDGE_LOCAL_PATH,
    env.PLUGIN_DATA_DIR,
    env.SANDBOX_ROOT,
    env.LOG_DIR,
  ]
  assert.equal(new Set(values).size, values.length, '六项落盘路径不该有重合')
})

test('dataDir 缺失时不编路径：宁可按子进程默认值失败，也不塞相对路径', () => {
  const env = stack.buildEngineEnv(
    { pgDsn: 'postgresql://x' },
    { appSecret: VALID_SECRET, port: 9100, env: {} },
  )
  for (const name of ['SKILL_STORE_PATH', 'MEDIA_STORE_PATH', 'LOG_DIR']) {
    assert.equal(env[name], undefined, `${name} 不该在拿不到数据目录时凭空出现`)
  }
})

test('显式传入的同名 env 优先（单测/运维会把路径钉到临时目录）', () => {
  const pinned = path.join(path.sep, 'tmp', 'pinned-skills')
  const env = stack.buildEngineEnv(
    { pgDsn: 'postgresql://x' },
    { appSecret: VALID_SECRET, port: 9100, dataDir: DATA_DIR, env: { SKILL_STORE_PATH: pinned } },
  )
  assert.equal(env.SKILL_STORE_PATH, pinned)
  // 没被显式覆盖的项仍然由壳层补齐
  assert.ok(env.MEDIA_STORE_PATH.startsWith(DATA_DIR))
})

test('buildGatewayEnv 把数据根交给网关：CHIRON_DATA_DIR / STORAGE_ROOT / PLUGIN_DATA_DIR', () => {
  const env = gatewayEnv()
  // 网关的 `cmd/chiron/main.go` 不解析 --data-dir，只能靠环境变量
  assert.equal(env.CHIRON_DATA_DIR, DATA_DIR, '网关认 CHIRON_DATA_DIR，缺了它就退回 ~/.chiron')
  assert.equal(env.STORAGE_ROOT, DATA_DIR, '媒体在 <StorageRoot>/media，给数据根本身')
  assert.ok(path.isAbsolute(env.PLUGIN_DATA_DIR))
  assert.ok(env.PLUGIN_DATA_DIR.startsWith(DATA_DIR))
})

test('探测自带可写数据目录：cwd 只读不该被判成"依赖没装好"', () => {
  const env = probeDataEnv(path.join(path.sep, 'tmp'))
  for (const name of [
    'SKILL_STORE_PATH',
    'MEDIA_STORE_PATH',
    'KNOWLEDGE_LOCAL_PATH',
    'PLUGIN_DATA_DIR',
    'SANDBOX_ROOT',
    'LOG_DIR',
  ]) {
    assert.ok(env[name], `探测缺 ${name}`)
    assert.ok(path.isAbsolute(env[name]), `${name} 必须是绝对路径，实际：${env[name]}`)
  }
})

test('引擎源码：导入期不许构造 SkillStore（只读安装目录下 mkdir 会失败）', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '..', '..', '..', 'python-engine', 'app', 'tools', 'skill.py'),
    'utf8',
  )
  // 模块级 `_store = SkillStore()` 这一行就是事故本身：改成惰性构造才允许 import
  assert.ok(
    !/^_store\s*=\s*SkillStore\(/m.test(source),
    'app/tools/skill.py 不能在模块级构造 SkillStore —— import 期会 mkdir',
  )
  assert.match(source, /^_store: SkillStore \| None = None$/m)
  assert.match(source, /def _shared_store\(\)/)
})
