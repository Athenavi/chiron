import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * M52c：会话事件流改走 `fetch` + `ReadableStream`。
 *
 * 直接起因是打包桌面态「发消息后一直停在思考中」：`EventSource` **不能设置请求头**，
 * 于是 `/events` 永远带不出 Authorization（本地身份是 `/v1/auth/session` 换到的 JWT），
 * 网关 401 后浏览器的原生重连把失败藏了起来 —— 前端既不报错也不出字。
 *
 * 这里钉住换实现后最容易丢的事：身份在 header（URL 里不再有 token）、非 2xx **立即**
 * 失败（`loading` 必须收得掉）、帧解析的边界语义（跨块、CRLF、`id:`、命名事件不派发）、
 * 重连携带 Last-Event-ID 且有上限、`close()` 之后一律静默 —— 以及**两条**长连接
 * （会话 `/events` 与用户级待办流）共用同一份实现，不许一处修好、另一处继续用 EventSource。
 */

type Host = {
  isDesktop?: boolean
  mode?: 'local' | 'cloud'
  deviceToken?: string
  apiUrl?: string
}

type ApiModule = typeof import('../index')

/** `api/index.ts` 在 import 时就快照了 `chironDesktop`，所以每次都要换新模块实例。 */
async function loadApi(host: Host): Promise<ApiModule> {
  vi.resetModules()
  ;(globalThis as { chironDesktop?: Host }).chironDesktop = host
  return await import('../index')
}

type Call = { url: string; init: Record<string, unknown> }

const encoder = new TextEncoder()

/** 依次返回这些响应的 fetch 桩（用完后一直返回最后一个）。 */
function stubFetch(...makeResponse: Array<() => unknown>) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: Record<string, unknown>) => {
    calls.push({ url, init })
    const index = Math.min(calls.length - 1, makeResponse.length - 1)
    return makeResponse[index]()
  }))
  return calls
}

function headersOf(call: Call): Record<string, string> {
  return (call.init.headers ?? {}) as Record<string, string>
}

/** 一条会依次吐出这些文本块、然后正常收尾的流。 */
function streamResponse(chunks: string[]) {
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => {
        let i = 0
        return {
          read: async () =>
            i < chunks.length
              ? { done: false, value: encoder.encode(chunks[i++]) }
              : { done: true, value: undefined },
        }
      },
    },
  }
}

/** 悬着的流：`read()` 一直不返回，由测试决定何时让它失败。 */
function pendingResponse() {
  let reject: (err: unknown) => void = () => {}
  return {
    response: {
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: () => new Promise((_resolve, rj) => { reject = rj }),
        }),
      },
    },
    breakStream: (err: unknown) => reject(err),
  }
}

function errorResponse(status: number) {
  // 真实网关的 401 **是带响应体的**（一段 JSON），所以这里 body 不能留 null：
  // 只判 `!response.body` 的写法在 null 上恰好也对，那条"错误体当流读"的退路就测不出来。
  return {
    ok: false,
    status,
    body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
  }
}

afterEach(() => {
  delete (globalThis as { chironDesktop?: Host }).chironDesktop
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it('本地模式：身份走 Authorization 头，URL 里不再出现 token', async () => {
  const mod = await loadApi({
    isDesktop: true,
    mode: 'local',
    deviceToken: 'dev-token',
    apiUrl: 'http://127.0.0.1:918/v1',
  })
  const calls = stubFetch(() => streamResponse([]))

  mod.createSSEConnection('s-1', vi.fn(), vi.fn())

  await vi.waitFor(() => expect(calls).toHaveLength(1))
  expect(headersOf(calls[0]).Authorization).toBe('Bearer dev-token')
  expect(calls[0].url).toContain('session_id=s-1')
  // 旧实现把设备 token 拼进 query（EventSource 没法设 header 才被迫如此）；
  // 它会出现在浏览器历史与访问日志里，现在必须消失。
  expect(calls[0].url).not.toContain('token=')
  // 桌面端 file:// 是跨站：cookie 带不过去，也不能指望它
  expect(calls[0].init.credentials).toBe('same-origin')
  // 首连不带续传头：网关把 `Last-Event-ID` 放在 query 之前解析（internal/api/events.go:49），
  // 发一个**空值**头等于要求"从空游标续传" —— 只在真有 id 时才设这个头。
  expect(headersOf(calls[0])['Last-Event-ID']).toBeUndefined()
})

it('云端模式：带登录 token，同源请求继续带 cookie', async () => {
  const mod = await loadApi({ isDesktop: false, mode: 'cloud', apiUrl: 'https://api.chiron.test/v1' })
  mod.setAuthToken('cloud-jwt')
  const calls = stubFetch(() => streamResponse([]))

  mod.createSSEConnection('s-9', vi.fn(), vi.fn())

  await vi.waitFor(() => expect(calls).toHaveLength(1))
  expect(headersOf(calls[0]).Authorization).toBe('Bearer cloud-jwt')
  expect(calls[0].init.credentials).toBe('include')
})

it('非 2xx 立刻失败：不重连、不派发，onError 恰好一次', async () => {
  // 这条是「思考中」的直接解药：401/403 不会自己好，重试只会把失败拖成无限等待。
  const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:918/v1' })
  const calls = stubFetch(() => errorResponse(401))
  const onMessage = vi.fn()
  const onError = vi.fn()

  mod.createSSEConnection('s-1', onMessage, onError, { autoReconnect: true, maxAutoReconnects: 3 })

  await vi.waitFor(() => expect(onError).toHaveBeenCalledTimes(1))
  expect(calls).toHaveLength(1)
  expect(onMessage).not.toHaveBeenCalled()
})

it('CRLF 帧：帧边界跨块也能拼回来', async () => {
  // SSE 允许 `\r\n` 当行分隔；归一必须做在**累计缓冲**上，否则第二块开头的 `\n`
  // 配不上第一块结尾的 `\r`，整条流永远不会出现帧边界 —— 表现为"连上了但不出字"。
  const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:918/v1' })
  stubFetch(() =>
    streamResponse(['id: 1\r\ndata: {"a":1}\r', '\n\r\nid: 2\r\ndata: {"b":2}\r\n\r\n']),
  )
  const messages: unknown[] = []

  mod.createSSEConnection('s-1', (d) => messages.push(d), vi.fn())

  await vi.waitFor(() => expect(messages).toHaveLength(2))
  expect(messages).toEqual([{ a: 1 }, { b: 2 }])
})

it('帧解析：跨块拼接、id 上报、命名事件与注释不派发', async () => {
  const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:918/v1' })
  stubFetch(() =>
    streamResponse([
      'id: 7\ndata: {"a":1}\n\n',
      'data: {"b"', // 半帧：边界落在下一个块里
      ':2}\n\n',
      // 本地 core 兜底通道发的就是命名事件；旧 onmessage 收不到，这里也必须跳过
      'event: heartbeat\ndata: {"seq":1}\n\n',
      ': keep-alive\n\n',
      'data: {坏 JSON\n\n', // 单帧坏掉不该断流
      'id: 8\ndata: {"c":3}\n\n',
    ]),
  )
  const messages: unknown[] = []
  const ids: string[] = []

  mod.createSSEConnection('s-1', (d) => messages.push(d), vi.fn(), {
    onLastEventId: (id) => ids.push(id),
  })

  await vi.waitFor(() => expect(messages).toHaveLength(3))
  expect(messages).toEqual([{ a: 1 }, { b: 2 }, { c: 3 }])
  expect(ids).toEqual(['7', '8'])
})

it('断线重连：携带 Last-Event-ID，次数用尽才报错', async () => {
  vi.useFakeTimers()
  const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:918/v1' })
  const calls = stubFetch(
    () => streamResponse(['id: 42\ndata: {"a":1}\n\n']),
    () => streamResponse([]),
  )
  const onError = vi.fn()

  mod.createSSEConnection('s-1', vi.fn(), onError, { autoReconnect: true, maxAutoReconnects: 3 })

  await vi.runAllTimersAsync()

  expect(calls).toHaveLength(4) // 1 次首连 + 3 次重连上限
  expect(headersOf(calls[1])['Last-Event-ID']).toBe('42')
  expect(onError).toHaveBeenCalledTimes(1)
})

it('跨轮续传：initialLastEventId 进 header 而不是 query', async () => {
  const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:918/v1' })
  const calls = stubFetch(() => streamResponse([]))

  mod.createSSEConnection('s-1', vi.fn(), vi.fn(), { initialLastEventId: 'x-99' })

  await vi.waitFor(() => expect(calls).toHaveLength(1))
  expect(headersOf(calls[0])['Last-Event-ID']).toBe('x-99')
  expect(calls[0].url).not.toContain('last_event_id=')
})

it('close() 之后一律静默：不报错、不重连，且可重复调用', async () => {
  const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:918/v1' })
  const pending = pendingResponse()
  const calls = stubFetch(() => pending.response)
  const onError = vi.fn()
  const onMessage = vi.fn()

  const handle = mod.createSSEConnection('s-1', onMessage, onError, { autoReconnect: true })
  await vi.waitFor(() => expect(calls).toHaveLength(1))

  handle.close()
  handle.close() // 幂等：卸载/切换会话会连着调
  // 静默只是"不看"，请求本身也必须掐掉：网关那条流不 abort 就一直挂在服务端，
  // 切会话时旧订阅还在收 —— 靠 closed 标志位挡不住服务端侧的资源占用。
  expect((calls[0].init.signal as AbortSignal).aborted).toBe(true)
  pending.breakStream(new Error('network changed'))

  await vi.waitFor(() => expect(calls).toHaveLength(1))
  expect(onError).not.toHaveBeenCalled() // 自己关的不算故障，否则切会话就弹「连接已断开」
  expect(onMessage).not.toHaveBeenCalled()
})

it('默认不自动重连：流一断就报错（旧语义）', async () => {
  const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:918/v1' })
  const calls = stubFetch(() => streamResponse([]))
  const onError = vi.fn()

  mod.createSSEConnection('s-1', vi.fn(), onError)

  await vi.waitFor(() => expect(onError).toHaveBeenCalledTimes(1))
  expect(calls).toHaveLength(1)
})

it('用户级待办流：同一条实现，身份只进 header，断线自己续', async () => {
  // `/v1/agent/interactions/stream` 也在 authMW 之后（internal/api/routes_agent.go:106）。
  // 它以前是 EventSource —— 打包桌面态里静默 401，角标只剩轮询兜底（延迟 30s 的"实时提醒"）。
  vi.useFakeTimers()
  await loadApi({
    isDesktop: true,
    mode: 'local',
    deviceToken: 'dev-token',
    apiUrl: 'http://127.0.0.1:918/v1',
  })
  const interactions = await import('../interactions')
  const calls = stubFetch(() => streamResponse([]))

  const handle = interactions.createUserEventsConnection(vi.fn())
  // 角标这条要长期挂着（旧行为是 EventSource 原生重连）：流一断就该再连，
  // 而不是静默到下一次轮询 —— 这里只看"确实重连了"，上限那条由上面的用例钉。
  await vi.runAllTimersAsync()
  expect(calls.length).toBeGreaterThan(1)
  expect(headersOf(calls[0]).Authorization).toBe('Bearer dev-token')
  expect(calls[0].url).toContain('/v1/agent/interactions/stream?client_id=ui-')
  expect(calls[0].url).not.toContain('token=')
  handle.close() // 不关掉就会把重连定时器留给下一个用例
})

it('护栏：两条事件流都不许再退回 EventSource', () => {
  // EventSource 无法携带 Authorization —— 换回它就是把它上面那条 401 重新静默化。
  // vitest 的 cwd 是 frontend-vue（不能用 import.meta.url —— 在 vite-node 下它不是 file: scheme）
  for (const rel of ['src/api/index.ts', 'src/api/interactions.ts']) {
    const source = readFileSync(resolve(process.cwd(), rel), 'utf8')
    expect(source, rel).not.toContain('new EventSource')
  }
})
