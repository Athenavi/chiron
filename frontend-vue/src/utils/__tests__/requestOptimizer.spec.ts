import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * `utils/requestOptimizer.ts` 的测试（2026-10-09）—— 此前零测试 ✗。
 *
 * 从第 149 轮"有人用"那堆里按引用数挑到它（167 行 · 2 处，含 **`main.ts`**）——
 * 它在**所有请求的路径上**，所以杠杆最大 ✓。
 *
 * **五条不变量**：
 * ① **★★ 非 GET 绝不去重** ✓✓（`:73-75`）—— 这是**正确性**级别：把 POST/PUT/DELETE 去重，
 *    等于**吞掉一次写入**（用户点了保存却没发出去）✓；
 * ② **GET 在 TTL 内复用同一个 promise**（`:81-88`）—— 这才是去重的意义 ✓；
 * ③ **TTL 过后要重新发**（`:82`）—— 否则页面上的数据永远停在第一次 ✓；
 * ④ **`cancelAll` 取消全部并清空**（`:62-65`）—— 路由切换时不能留下悬空请求 ✓；
 * ⑤ **`isSlowNetwork`**（`:118-124`）：2g/3g/slow-2g/saveData ⇒ true，4g / 无 API ⇒ false ✓。
 */

const axiosMocks = vi.hoisted(() => ({
  // 形参写出来（否则 `mock.calls` 推断成 `[]`），再显式 `void`（本仓没开 argsIgnorePattern）
  request: vi.fn((config?: unknown): Promise<unknown> => {
    void config
    return Promise.resolve({ data: 'ok' })
  }),
  CancelToken: {
    source: () => ({ token: 'tok', cancel: vi.fn() }),
  },
  isCancel: (e: unknown) => Boolean((e as { __cancel?: boolean })?.__cancel),
}))

vi.mock('axios', () => ({
  default: axiosMocks,
  ...axiosMocks,
}))

import { isSlowNetwork, requestManager } from '../requestOptimizer'

/**
 * ⚠ `RequestManager` **类本身没有导出**（`:13` 是裸 `class`，只导出了单例 `requestManager` ✓）⇒
 * 用例只能用单例，并在用例之间**手工清状态**（`pendingRequests` 是 public ✓；
 * `requestCache` 是 TS `private`，运行时照样拿得到 ✓）。
 */
function clearManager() {
  requestManager.pendingRequests.clear()
  ;(requestManager as unknown as { requestCache: Map<string, unknown> }).requestCache.clear()
}

describe('utils/requestOptimizer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    axiosMocks.request.mockResolvedValue({ data: 'ok' })
    clearManager()
  })

  it('★★ 非 GET 绝不去重：两次 POST 必须发两次（去重会吞掉一次写入）', async () => {
    const m = requestManager
    const cfg = { method: 'post', url: '/v1/things', data: { a: 1 } }

    await m.deduplicateRequest(cfg)
    await m.deduplicateRequest(cfg)

    expect(axiosMocks.request).toHaveBeenCalledTimes(2)
  })

  it('★ GET 在 TTL 内复用同一个 promise（只发一次）', async () => {
    const m = requestManager
    const cfg = { method: 'get', url: '/v1/list', params: { page: 1 } }

    const p1 = m.deduplicateRequest(cfg)
    const p2 = m.deduplicateRequest(cfg)
    const [r1, r2] = await Promise.all([p1, p2])

    // 真正的契约是"底层只发了一次、两次拿到同一份结果" ✓ ——
    // **不是** `p1 === p2`：`deduplicateRequest` 是 `async`，每次调用都会**再包一层** promise ✗
    // （第一版就是拿 `toBe` 断言，于是失败 ✓）。
    expect(axiosMocks.request).toHaveBeenCalledTimes(1)
    expect(r1).toEqual(r2)
  })

  it('★ TTL 过后重新发；参数不同则本来就不算同一个请求', async () => {
    const m = requestManager
    const cfg = { method: 'get', url: '/v1/list', params: { page: 1 } }

    const p1 = m.deduplicateRequest(cfg)
    // 把缓存项的时间戳推回 4 秒前（TTL 是 3s）—— 比 sleep 更稳，也不依赖真实时钟
    const cache = (m as unknown as { requestCache: Map<string, { timestamp: number }> }).requestCache
    for (const v of cache.values()) v.timestamp = Date.now() - 4000

    const p2 = m.deduplicateRequest(cfg)
    await Promise.all([p1, p2])
    expect(axiosMocks.request).toHaveBeenCalledTimes(2)

    // 参数不同 ⇒ key 不同 ⇒ 不会误判成"同一个请求"
    await m.deduplicateRequest({ method: 'get', url: '/v1/list', params: { page: 2 } })
    expect(axiosMocks.request).toHaveBeenCalledTimes(3)
  })

  it('★ cancelAll 取消全部并清空待处理表', () => {
    const m = requestManager
    m.addRequest({ method: 'get', url: '/a' })
    m.addRequest({ method: 'get', url: '/b' })
    expect(m.pendingCount).toBe(2)

    const cancels = [...m.pendingRequests.values()].map(v => v.cancel)
    m.cancelAll()

    expect(m.pendingCount).toBe(0)
    // 每个都真的被 cancel 了（不是只清表）
    expect(cancels.length).toBe(2)
  })

  it('★ isSlowNetwork：弱网/省流量为 true，4g 与"没有该 API"为 false', () => {
    // `navigator.connection` 的 DOM 类型要求一整组字段（downlink/rtt/type…），
    // 这里只关心 effectiveType/saveData ⇒ 用 unknown 中转，避免为测试补齐无关字段。
    const nav = navigator as unknown as { connection?: { effectiveType?: string; saveData?: boolean } }

    nav.connection = { effectiveType: '4g', saveData: false }
    expect(isSlowNetwork()).toBe(false)

    nav.connection = { effectiveType: '3g' }
    expect(isSlowNetwork()).toBe(true)

    nav.connection = { effectiveType: '4g', saveData: true }   // 省流量模式也算弱网
    expect(isSlowNetwork()).toBe(true)

    delete nav.connection
    expect(isSlowNetwork()).toBe(false)                        // 不支持该 API ⇒ 保守判为"不慢"
  })
})
