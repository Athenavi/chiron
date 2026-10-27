import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  createTallyCache,
  scheduleTally,
  tallyCache,
  TALLY_DELAY_MS,
  TALLY_TTL_MS,
} from '../tallyCache'

/**
 * `utils/tallyCache.ts` 的测试 —— 四条规则**每条一个用例**，另外补两条
 * 容易写错却没人测的（`invalidate` 与「延后期间被取消」）。
 *
 * 四条规则来自上游 `f41b9d413`（打开设置不再为存储总量走磁盘）的做法：
 * ① 延后 ② TTL 缓存 ③ 并发合并 ④ 失败不缓存。
 * 这里逐一钉住，因为**任何一条单独失效都不会报错**，只会把昂贵请求悄悄发回去。
 */

describe('utils/tallyCache', () => {
  beforeEach(() => {
    tallyCache.invalidate()
  })

  afterEach(() => {
    vi.useRealTimers()
    tallyCache.invalidate()
  })

  it('① 延后：面板打开后不立刻发请求，等满 delay 才发；已有缓存则立即给值、不发', async () => {
    vi.useFakeTimers()
    const cache = createTallyCache()
    const fetcher = vi.fn(async () => ['conv-1', 'conv-2'])
    const got: string[][] = []

    scheduleTally(cache, 'conversations', fetcher, v => got.push(v), { delayMs: TALLY_DELAY_MS })

    // 刚打开面板：「路过」不该产生任何请求
    expect(fetcher).not.toHaveBeenCalled()
    expect(got).toEqual([])

    // 差 1ms 到点，仍然不该发
    await vi.advanceTimersByTimeAsync(TALLY_DELAY_MS - 1)
    expect(fetcher).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(got).toEqual([['conv-1', 'conv-2']])

    // 再次打开面板：缓存新鲜 ⇒ 立即渲染，不等 1.5 秒、不再发请求
    const second: string[][] = []
    scheduleTally(cache, 'conversations', fetcher, v => second.push(v), { delayMs: TALLY_DELAY_MS })
    expect(second).toEqual([['conv-1', 'conv-2']])
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('② TTL 命中：TTL 内不重复请求，过期后才重取；force 绕过缓存', async () => {
    const cache = createTallyCache()
    let now = 1_000
    const fetcher = vi.fn(async () => 7)

    expect(await cache.load('shares', fetcher, { now: () => now })).toBe(7)
    expect(fetcher).toHaveBeenCalledTimes(1)

    // TTL 差 1ms 未过期 ⇒ 命中缓存，不发请求
    now += TALLY_TTL_MS - 1
    expect(await cache.load('shares', fetcher, { now: () => now })).toBe(7)
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(cache.peek<number>('shares', { now: () => now })).toBe(7)

    // 过期 ⇒ 重新发
    now += 2
    expect(await cache.load('shares', fetcher, { now: () => now })).toBe(7)
    expect(fetcher).toHaveBeenCalledTimes(2)

    // 手动刷新路径：force 必须绕过缓存（否则「刷新」按钮点了等于没点）
    await cache.load('shares', fetcher, { now: () => now, force: true })
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('③ 并发合并：同 key 同时在飞只发一次，所有调用者拿到同一结果', async () => {
    const cache = createTallyCache()
    let release: (v: number) => void = () => { /* 由 Promise 构造函数填充 */ }
    const fetcher = vi.fn(() => new Promise<number>((resolve) => { release = resolve }))

    const a = cache.load('identities', fetcher, { now: () => 1 })
    const b = cache.load('identities', fetcher, { now: () => 1 })
    const c = cache.load('identities', fetcher, { now: () => 1 })

    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(cache.inFlight('identities')).toBe(true)

    release(42)
    expect(await Promise.all([a, b, c])).toEqual([42, 42, 42])
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(cache.inFlight('identities')).toBe(false)
  })

  it('④ 失败不缓存：失败后清掉 in-flight，下一次真的重试并能成功', async () => {
    const cache = createTallyCache()
    const fetcher = vi
      .fn<() => Promise<string[]>>()
      .mockRejectedValueOnce(new Error('upstream busy'))
      .mockResolvedValueOnce(['ok'])

    await expect(cache.load('phone', fetcher, { now: () => 1 })).rejects.toThrow('upstream busy')

    // 失败**没有**被记成结果：in-flight 已清、peek 无值 ⇒ 下次会重新发
    expect(cache.inFlight('phone')).toBe(false)
    expect(cache.peek('phone', { now: () => 1 })).toBeUndefined()

    expect(await cache.load('phone', fetcher, { now: () => 1 })).toEqual(['ok'])
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('invalidate 作废单个 key 与全部；作废后必须重取（数据变更后的显式失效）', async () => {
    const cache = createTallyCache()
    const a = vi.fn(async () => 'A')
    const b = vi.fn(async () => 'B')

    await cache.load('a', a, { now: () => 1 })
    await cache.load('b', b, { now: () => 1 })
    expect(a).toHaveBeenCalledTimes(1)

    cache.invalidate('a')
    await cache.load('a', a, { now: () => 2 })
    await cache.load('b', b, { now: () => 2 })
    expect(a).toHaveBeenCalledTimes(2)   // a 被作废 ⇒ 重取
    expect(b).toHaveBeenCalledTimes(1)   // b 不受影响 ⇒ 仍命中

    cache.invalidate()                   // 不带 key ⇒ 全清（账号切换等全局事件）
    await cache.load('a', a, { now: () => 3 })
    await cache.load('b', b, { now: () => 3 })
    expect(a).toHaveBeenCalledTimes(3)
    expect(b).toHaveBeenCalledTimes(2)
  })

  it('延后期间组件卸载：取消后一个请求都不发（关掉的弹窗不该在 1.5 秒后开口）', async () => {
    vi.useFakeTimers()
    const cache = createTallyCache()
    const fetcher = vi.fn(async () => 'x')
    const cancel = scheduleTally(cache, 'k', fetcher, () => {})

    await vi.advanceTimersByTimeAsync(1_000)
    cancel()
    await vi.advanceTimersByTimeAsync(TALLY_DELAY_MS)

    expect(fetcher).not.toHaveBeenCalled()
  })
})
