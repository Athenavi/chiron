'use strict'
/**
 * 本地后端的「谁起的谁停」接线（M44）。
 *
 * 真机现场（Windows 安装包，本地模式）：每次退出后 `postgres` 与 `redis-server`
 * 仍在监听，日志却写着 `local backend stopped` —— 报告了一次根本没发生的回收。
 *
 * 根因：打包态的进程归引导器那套（`local-stack.cjs`）所有，而 `stopLocalBackend`
 * 只调 `components.stopCore`。那条链没起 core，`state.core` 为空 ⇒ stopCore 返回
 * false ⇒ 老实现照样打"stopped"。于是退出后 PG 还占着 data dir，下一次启动又拉起
 * 一套实例抢同一个目录（本轮能连续启动成功，只因为上一套是我手动清掉的）。
 *
 * 钉住的不变量（main.cjs 一 require 就起 Electron，行为只能这样守 + 端到端验）：
 *   1. 启动成功把进程归属记进 `localBackend.via`（'go-stack' / 'core'）；
 *   2. 停止按 `via` 分派：go-stack 打到 local-stack.stopLocalStack，core 打到 components；
 *   3. 停不掉就不许声称停掉 —— via 为空、链路未知、停止函数不可用一律 return false，
 *      且成功日志全函数只允许出现在真停掉之后那一处；
 *   4. 停止先把状态复位（别让重建的窗口拿到死 baseUrl 和废 token），
 *      "只停一次"的标记由本函数自己维护，调用点不再各写一份。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const mainSource = fs
  .readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')
  .replace(/\r\n/g, '\n')

/** 取某个顶层函数的源码体（从声明处到下一个顶格 `}`） */
function functionBody(source, declaration) {
  const start = source.indexOf(declaration)
  assert.ok(start > 0, `源码里找不到 ${declaration}`)
  const end = source.indexOf('\n}\n', start)
  assert.ok(end > start, `${declaration} 之后找不到函数结束`)
  return source.slice(start, end)
}

const stopBody = functionBody(mainSource, 'async function stopLocalBackend')
const goStackBody = functionBody(mainSource, 'async function tryStartGoStack')

/** 取某个 `if (...) { … }` 分支的源码体：按大括号配对取，别用"下一个顶格 }"——
 *  分支里的日志模板串带着 `${}`，靠缩进找收尾会一路吃到函数尾部。 */
function branchBody(source, condition) {
  const start = source.indexOf(condition)
  assert.ok(start > 0, `源码里找不到 ${condition}`)
  const open = source.indexOf('{', start + condition.length)
  let depth = 0
  let end = -1
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1
    else if (source[i] === '}') {
      depth -= 1
      if (depth === 0) {
        end = i
        break
      }
    }
  }
  assert.ok(end > open, `${condition} 之后找不到分支结束`)
  return source.slice(open, end)
}

// ------------------------------------------------------------------ 归属标记
test('★ 回归：停止按启动时记下的 via 分派，不再固定调 stopCore', () => {
  assert.match(stopBody, /const via = ownership\.via/, '归属取自入口那份快照，不是清空后的全局')
  assert.match(stopBody, /const stoppers = \{/, '停止函数要按链路做成一张表')
  assert.match(stopBody, /stoppers\[via\]/, '表做好了却没按 via 取')
  assert.match(stopBody, /stack\.stopLocalStack/, 'go-stack 那套的 owner 是 local-stack，不是 components')
  // components 只准出现在 core 分支里：一进函数就去载它 = 原 bug 的形状（谁起的都停 core）
  const prologue = stopBody.slice(0, stopBody.indexOf('const stoppers'))
  assert.doesNotMatch(prologue, /components\.cjs/, '分派之前就去载 components')
})

test('go-stack 的停止函数来自 local-stack.cjs', () => {
  const goBranch = stopBody.slice(stopBody.indexOf("'go-stack'"), stopBody.indexOf('core: ()'))
  assert.match(goBranch, /loadOptionalModule\('\.\/local-stack\.cjs'\)/)
  assert.match(goBranch, /typeof stack\.stopLocalStack === 'function'/)
  assert.match(goBranch, /: null/, '拿不到 stopLocalStack 要返回 null，不能默默当成功')
})

test('两条链路的启动结果都写上归属', () => {
  assert.match(goStackBody, /via:\s*'go-stack'/, '主链路没标归属，停止时就找不到该叫谁')
  assert.match(mainSource, /via:\s*'core'/, 'core 回退同样要标，否则照样停不掉')
})

test('空态工厂里有 via 位；声明与停止后的复位都走它', () => {
  assert.match(mainSource, /function blankLocalBackend\(\)[\s\S]{0,160}?via:\s*''/)
  assert.match(mainSource, /let localBackend = blankLocalBackend\(\)/, '初始态没走工厂，字段又要各写一份')
  assert.match(stopBody, /localBackend = blankLocalBackend\(\)/, '停完不复位：重建的窗口会拿到死地址和废 token')
})

// ------------------------------------------------------------------ 不许谎报
test('没成功起过就直说：不打 stopped，也不去找停止函数', () => {
  const head = stopBody.slice(0, stopBody.indexOf('const stoppers'))
  assert.match(head, /if \(!via\)/)
  assert.match(head, /return false/)
  assert.doesNotMatch(head, /local backend stopped/)
  // 归属不许兜默认值：`via || 'go-stack'` 会把"从没起来"当成"起过 go-stack"，
  // 于是没启动也去调 stopLocalStack，成功日志照样打 stopped。
  assert.doesNotMatch(head, /via\s*\|\|/, '给归属兜了个默认值')
})

test('链路未知 / 停止函数不可用：warn 并返回 false', () => {
  // 各自取分支体内的那一句 return，别用"后面某处有 return false"糊过去——
  // 两条分支只隔几行，宽松写法会把"静默当成功"的变异放过去。
  const unknown = branchBody(stopBody, 'if (!stopper)')
  assert.match(unknown, /未被回收/)
  assert.match(unknown, /return false/)
  assert.doesNotMatch(unknown, /return true/)
  const unavailable = branchBody(stopBody, 'if (result === null)')
  assert.match(unavailable, /未被回收/)
  assert.match(unavailable, /return false/)
  assert.doesNotMatch(unavailable, /return true|\bstopped\b/, '停不掉却打 stopped = 原 bug 的翻版')
})

test('成功日志全函数只有一处，且带上链路和逐条回收结果', () => {
  assert.equal(
    [...stopBody.matchAll(/local backend stopped/g)].length,
    1,
    '出现两处成功日志：多出来的那处一定是"没真停也报 stopped"',
  )
  assert.match(stopBody, /local backend stopped \$\{via\}\$\{detail\}/)
  assert.match(stopBody, /gateway=.*engine=.*launcher=/, 'go-stack 的三条回收结果要可见')
  assert.match(stopBody, /core=\$\{Boolean\(result\)\}/, 'core 的布尔结果要可见')
})

test('入口闸门只看启动成功标记，不看归属', () => {
  // 归属是 ok 之后才写的从属信息；拿它当闸门等于把同一条不变量抄两份，
  // 而且"从没起来"会被当成"起过但停不掉"，正好掩盖原 bug。
  assert.match(stopBody, /if \(localBackendStopped \|\| !localBackend\.ok\) return false/)
  const gate = stopBody.slice(0, stopBody.indexOf('\n', stopBody.indexOf('return false')))
  assert.doesNotMatch(gate, /ownership|\.via/, '闸门引用了归属')
})

// ------------------------------------------------------------------ 调用点
test('退出前先 preventDefault，停完再真的 quit', () => {
  const quitAt = mainSource.indexOf("before-quit'")
  assert.ok(quitAt > 0, '找不到 before-quit 接线')
  const quitBlock = mainSource.slice(quitAt, quitAt + 700)
  assert.match(quitBlock, /event\.preventDefault\(\)/)
  assert.match(quitBlock, /void stopLocalBackend\(\)/)
  assert.match(quitBlock, /app\.quit\(\)/, '停完再退，否则 app.quit 会绕过清理')
})

test('「只停一次」的标记只有 stopLocalBackend 一处置位', () => {
  const sets = [...mainSource.matchAll(/localBackendStopped = true/g)].length
  assert.equal(sets, 1, '调用点各置一次 = 第二个状态真值来源，改一处忘一处就又回到静默没停')
})

test('从主界面重启本地后端：先停、再把「只停一次」标记复位，然后带进度窗起', () => {
  const at = mainSource.indexOf("ipcMain.handle('desktop:restart-local-backend'")
  assert.ok(at > 0, '找不到重启本地后端的接线')
  const block = mainSource.slice(at, at + 700)
  const stopAt = block.indexOf('await stopLocalBackend()')
  const resetAt = block.indexOf('localBackendStopped = false')
  const startAt = block.indexOf('startLocalBackendWithProgress()')
  assert.ok(stopAt > 0 && resetAt > stopAt && startAt > resetAt, '停止 → 复位 → 重启，顺序不能换：不复位下次退出就不会停')
})
