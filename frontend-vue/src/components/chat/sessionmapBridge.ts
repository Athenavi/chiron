/**
 * 会话地图 → 宿主的 RPC 桥。
 *
 * 为什么需要它
 * ------------
 * 地图（`public/sessionmap/app.js`）用 `dshRpc(type, payload)` 发起**会话操作**，
 * 而它自己的实现是「postMessage 给父窗口 + 等一条带 `requestId` 的回执」
 * （app.js:142 定义、app.js:2070 结算、20 秒超时）：
 *
 *     if (data.type === 'synapse:forked-session' || 'synapse:created-session' ||
 *         'synapse:message-sent') settleRpc(data.requestId, data.session ?? data)
 *     if (data.type === 'synapse:bridge-error') settleRpc(data.requestId, undefined, Error(data.message))
 *
 * 此前宿主（ChatView）只实现了 `synapse:activate-session`（切会话），**没有实现这三条回执** ——
 * 于是地图里「新建会话 / 发送追问 / 分叉」全部空等 20 秒后报
 * 「会话服务未在规定时间内响应」。这就是"地图中的会话无法产生有效请求"的根因。
 *
 * 为什么不让地图自己直连 REST（adapter.js 里原本有一份实现）：因为 `dshRpc` 是 app.js 的
 * **顶层函数声明**，它会把 `window.dshRpc` 覆盖掉 —— 那份适配从未生效，而且即使生效，
 * 地图发出的消息也不会经过宿主，宿主与地图的会话状态会各说各话。改由宿主代理，
 * 会话的主人始终是宿主（地图是会话的投影/引用）。
 *
 * 设计：本模块只做**编排与回执**，所有副作用经 {@link MapRpcHost} 注入 —— 于是它能在
 * 单测里被完整验证（不需要浏览器、不需要 Vue 组件）。
 */

/** 地图发起的 RPC 类型（与 app.js 的 `dshRpc(...)` 调用一一对应）。 */
export const MAP_RPC_TYPES = [
  'synapse:create-session',
  'synapse:send-message',
  'synapse:fork-session',
  // 重命名 / 备注别名 / 标签：地图上的会话元数据**必须经宿主**写（会话的主人是宿主），
  // 这样对话页、侧边栏、地图三处的显示立刻一致。
  'synapse:update-session',
] as const

export type MapRpcType = (typeof MAP_RPC_TYPES)[number]

/** 地图发来的一条 RPC 请求（`requestId` 由地图生成，回执必须原样带回）。 */
export interface MapRpcRequest {
  type?: string
  requestId?: string
  sessionId?: string
  text?: string
  mode?: string
  atSeq?: number
  title?: string
  /** 会话别名/备注（update-session 用） */
  alias?: string
  /** 会话标签（update-session 用） */
  tag?: string
  workspaceId?: string
  cwd?: string
}

/** 会话元数据补丁：只带要改的字段（空串 = 清除该字段）。 */
export interface SessionMetaPatch {
  title?: string
  alias?: string
  tag?: string
}

/** 宿主提供的能力。返回值形状是 app.js 的契约：`{id, title}`。 */
export interface MapRpcHost {
  /** 把一条消息推回地图（宿主侧是 postMessage，测试里是数组）。 */
  post: (payload: Record<string, unknown>) => void
  /** 新建会话（宿主创建 → 地图会把它摆上画布）。 */
  createSession: (title: string) => Promise<{ id: string; title?: string }>
  /** 在**指定会话**上发消息；`mode === 'followup'` 表示"追问"（一次性简短回复）。 */
  sendMessage: (sessionId: string, text: string, mode: string) => Promise<void>
  /** 从某条消息处分叉出一个新会话。 */
  forkSession: (
    sessionId: string,
    atSeq: number,
    title: string,
  ) => Promise<{ id: string; title?: string }>
  /** 更新会话元数据（重命名 / 备注别名 / 标签）；空串表示清除。 */
  updateSession: (sessionId: string, patch: SessionMetaPatch) => Promise<void>
}

/**
 * 追问的提示词格式：与地图侧（`public/sessionmap/adapter.js` 的 `FOLLOWUP_RE`）**成对演进**。
 *
 * 历史数据里还可能有**带外层全角括号**的写法（早先 adapter 自己发消息时用的格式），
 * 所以 adapter 的正则两种都认 —— 改这里的格式前先确认那边仍然匹配。
 */
export const FOLLOWUP_TEMPLATE = (text: string) => `【请简短回答问题】:(${text})`

export function isMapRpc(type: unknown): type is MapRpcType {
  return typeof type === 'string' && (MAP_RPC_TYPES as readonly string[]).includes(type)
}

function messageOf(err: unknown): string {
  if (err instanceof Error) return err.message
  return String(err ?? '会话服务调用失败')
}

/**
 * 处理一条来自地图的消息；返回是否已接管（`false` 表示不是 RPC，调用方继续走自己的分支）。
 *
 * 失败**必须**回 `synapse:bridge-error`：地图那边的 promise 只有收到回执才会 settle，
 * 否则用户只能等 20 秒超时看到一句笼统的"未响应"（这正是本次要消灭的体验）。
 */
export function handleMapRpc(data: MapRpcRequest | null | undefined, host: MapRpcHost): boolean {
  if (!data || !isMapRpc(data.type)) return false
  const type = data.type
  const requestId = data.requestId

  // 不 await：postMessage 的调用方不该被阻塞；错误在内部转成回执
  void (async () => {
    try {
      // ⚠️ 回执的 `type` 与请求的 `type` **不是同一个名字**（app.js 按回执名 settleRpc）：
      //   synapse:create-session → synapse:created-session
      //   synapse:send-message   → synapse:message-sent
      //   synapse:fork-session   → synapse:forked-session
      if (type === 'synapse:create-session') {
        const session = await host.createSession(String(data.title || '新对话'))
        host.post({ type: 'synapse:created-session', requestId, session })
        return
      }
      if (type === 'synapse:fork-session') {
        const sessionId = String(data.sessionId || '')
        if (!sessionId) throw new Error('缺少来源会话')
        const session = await host.forkSession(
          sessionId,
          Number(data.atSeq) || 1,
          String(data.title || ''),
        )
        host.post({ type: 'synapse:forked-session', requestId, session })
        return
      }
      if (type === 'synapse:update-session') {
        const sessionId = String(data.sessionId || '')
        if (!sessionId) throw new Error('缺少目标会话')
        const patch: SessionMetaPatch = {}
        if (typeof data.title === 'string') patch.title = data.title.trim()
        if (typeof data.alias === 'string') patch.alias = data.alias.trim()
        if (typeof data.tag === 'string') patch.tag = data.tag.trim()
        if (Object.keys(patch).length === 0) throw new Error('没有要更新的字段')
        await host.updateSession(sessionId, patch)
        // 回执类型由 app.js 的 settleRpc 认（补丁区块里加了这一条）
        host.post({ type: 'synapse:session-updated', requestId, session: { id: sessionId, ...patch } })
        return
      }
      // synapse:send-message
      const sessionId = String(data.sessionId || '')
      const text = String(data.text || '').trim()
      if (!sessionId) throw new Error('缺少目标会话')
      if (!text) throw new Error('消息内容为空')
      await host.sendMessage(sessionId, text, String(data.mode || ''))
      host.post({ type: 'synapse:message-sent', requestId, session: { id: sessionId } })
    } catch (err) {
      host.post({ type: 'synapse:bridge-error', requestId, message: messageOf(err) })
    }
  })()

  return true
}
