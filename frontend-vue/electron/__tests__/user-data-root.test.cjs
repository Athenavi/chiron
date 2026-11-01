'use strict'
/**
 * userData 根的判定契约。
 *
 * 背景（真机事故）：本地模式的全部数据都挂在 `app.getPath('userData')` 下面 ——
 * config.json、日志、两套 venv（`local/runtime`、`local/engine`）、PG 数据目录、
 * 单实例锁。而 Electron 的默认值取自 `package.json` 的 `name`，本项目是
 * `frontend-vue`，于是用户机器上出现的是 `%APPDATA%\frontend-vue\` ——
 * 一个既看不出属于 Chiron、也不该被 App 写入的目录名。
 *
 * 这里钉三件事：
 *   1. 默认根是**固定可识别名**，不再跟着 package.json 的 name 漂；
 *   2. `CHIRON_DESKTOP_USER_DATA` 仍然完全覆盖（真机 smoke / 多环境隔离要靠它）；
 *   3. `main.cjs` 必须在 `requestSingleInstanceLock()` **之前**改写
 *      —— 晚一步就会出现「锁在旧根、venv 在新根」的同一进程两套目录。
 *
 * 全部是纯函数 + 源码级守卫，不真的起 Electron（`main.cjs` 一 require 就会起）。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const shared = require('../shared.cjs')

test('默认根是可识别的固定名，不跟 package.json 的 name 走', () => {
  const resolved = shared.resolveUserDataDir({ appData: 'C:\\Users\\someone\\AppData\\Roaming' })
  assert.equal(
    path.basename(resolved),
    shared.USER_DATA_DIR_NAME,
    'userData 根必须用 shared 里那个固定名',
  )
  assert.equal(shared.USER_DATA_DIR_NAME, 'gh_Athenavi_chiron')
  // 名字里不该出现 Electron 默认的那个包名：那正是本次要摆脱的东西
  assert.ok(!resolved.includes('frontend-vue'), `不该再落到 frontend-vue 目录：${resolved}`)
})

test('落在 appData 之下，而不是另起一个绝对路径', () => {
  const appData = path.join(path.sep, 'tmp', 'roaming')
  assert.equal(shared.resolveUserDataDir({ appData }), path.join(appData, shared.USER_DATA_DIR_NAME))
})

test('CHIRON_DESKTOP_USER_DATA 仍然完全覆盖（绝对路径）', () => {
  const override = path.join(path.sep, 'tmp', 'chiron-smoke')
  assert.equal(shared.resolveUserDataDir({ appData: '/whatever', override }), override)
})

test('覆盖值两端空白被裁掉，纯空白当作没给', () => {
  assert.equal(shared.resolveUserDataDir({ appData: '/a', override: '  /kiosk/chiron  ' }), '/kiosk/chiron')
  assert.equal(
    shared.resolveUserDataDir({ appData: '/a', override: '   ' }),
    path.join('/a', shared.USER_DATA_DIR_NAME),
  )
})

test('拿不到 appData 时返回空串，让调用方保持 Electron 默认值', () => {
  // 宁可不动，也不要 setPath 一个空串 —— 那会让所有路径变成相对路径
  assert.equal(shared.resolveUserDataDir({}), '')
  assert.equal(shared.resolveUserDataDir({ appData: '' }), '')
  assert.equal(shared.resolveUserDataDir({ appData: null }), '')
})

test('main.cjs 必须在单实例锁之前改写 userData', () => {
  // 源码级守卫：这两句的相对顺序就是「锁与数据是否同一个根」的全部因果。
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')
  const setPath = source.indexOf("app.setPath('userData'")
  const lock = source.indexOf('app.requestSingleInstanceLock()')
  assert.ok(setPath > 0, 'main.cjs 应当改写 userData 根')
  assert.ok(lock > 0, 'main.cjs 应当仍然请求单实例锁')
  assert.ok(
    setPath < lock,
    'userData 必须在 requestSingleInstanceLock 之前定下来，否则锁在旧根、数据在新根',
  )
  // 改写必须走共享口径，别在 main.cjs 里另写一份目录名
  assert.match(source, /shared\.resolveUserDataDir\(/)
  assert.ok(
    !/['"]gh_Athenavi_chiron['"]/.test(source),
    '目录名只留在 shared.cjs，main.cjs 里写死第二份就会两处漂移',
  )
})
