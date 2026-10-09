import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { routeLoading, startProgress, stopProgress, setupRouteProgress } from '../progress'

/**
 * `router/progress.ts` 的测试（2026-10-09）—— 此前零测试 ✗。
 *
 * 承第 145 轮的扫描（`api / composables / utils / stores / router` 共 24 模块 / 2,631 行无测试）✓，
 * 本轮挑 `progress.ts`（121 行）：它是**纯逻辑 + 时序**，而且**防闪**的意图写在代码里 ✓。
 *
 * **三条不变量**：
 * ① **50ms 防闪**（`:8-11`）：`startProgress` 只是**预约**一个定时器，不是立刻置位 ⇒
 *    快导航（50ms 内结束）**根本不该看到进度条** ✓；
 * ② **`stopProgress` 必须把待定的定时器取消掉**（`:13-16`）—— 只置 `false` 而不 clear，
 *    定时器到点后会**再把它点亮**，于是"导航早就结束了、进度条却冒出来" ✓；
 * ③ **挂到 router 上的钩子三处都要收尾**（`:107-117`）：`beforeEach` 起、`afterEach` 与 `onError` 都要停 ✓ ——
 *    漏掉 `onError` 的话，**导航失败会永远转圈** ✓。
 */

describe('router/progress', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    routeLoading.value = false
  })

  afterEach(() => {
    stopProgress()
    vi.useRealTimers()
  })

  it('★ 50ms 防闪：快导航不该显示进度条', () => {
    startProgress()
    expect(routeLoading.value).toBe(false)      // 只是预约，没有立刻置位

    vi.advanceTimersByTime(20)                  // 还没到 50ms
    expect(routeLoading.value).toBe(false)

    stopProgress()                              // 导航在 50ms 内结束
    vi.advanceTimersByTime(200)                 // 把时间推过去
    expect(routeLoading.value).toBe(false)      // **仍然**不该显示
  })

  it('★ 慢导航才显示（50ms 之后）', () => {
    startProgress()
    vi.advanceTimersByTime(50)
    expect(routeLoading.value).toBe(true)

    stopProgress()
    expect(routeLoading.value).toBe(false)
  })

  it('★ stopProgress 会取消待定定时器（否则会在结束后又被点亮）', () => {
    startProgress()
    stopProgress()                    // 立刻结束
    vi.advanceTimersByTime(1000)      // 定时器若没被清掉，这里会把 loading 置 true
    expect(routeLoading.value).toBe(false)
  })

  it('★ 挂到 router 的钩子：afterEach 与 onError 都要停（漏 onError 会永远转圈）', () => {
    const hooks: Record<string, (...a: unknown[]) => unknown> = {}
    const fakeRouter = {
      beforeEach: (fn: (...a: unknown[]) => unknown) => { hooks.beforeEach = fn },
      afterEach: (fn: (...a: unknown[]) => unknown) => { hooks.afterEach = fn },
      onError: (fn: (...a: unknown[]) => unknown) => { hooks.onError = fn },
      getRoutes: () => [],
    }
    // 预加载那部分需要 window 事件与 idle 回调；本用例只关心三个钩子的收尾
    const origIdle = (globalThis as { requestIdleCallback?: unknown }).requestIdleCallback
    ;(globalThis as { requestIdleCallback?: unknown }).requestIdleCallback = undefined

    setupRouteProgress(fakeRouter as never)

    // beforeEach 起：走完 50ms 后确实亮了
    hooks.beforeEach!({}, {}, () => {})
    vi.advanceTimersByTime(50)
    expect(routeLoading.value).toBe(true)

    // afterEach 收
    hooks.afterEach!()
    expect(routeLoading.value).toBe(false)

    // onError 收（导航失败也必须停）
    hooks.beforeEach!({}, {}, () => {})
    vi.advanceTimersByTime(50)
    expect(routeLoading.value).toBe(true)
    hooks.onError!()
    expect(routeLoading.value).toBe(false)

    if (origIdle === undefined) delete (globalThis as { requestIdleCallback?: unknown }).requestIdleCallback
    else (globalThis as { requestIdleCallback?: unknown }).requestIdleCallback = origIdle
  })
})
