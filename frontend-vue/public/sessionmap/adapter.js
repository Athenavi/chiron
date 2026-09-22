/**
 * Chiron × dsh-synapse 缝合层
 * ============================
 *
 * `app.js` / `styles.css` 是**原样照抄** `vendor/dsh-synapse` 的前端（画布、卡片、SVG 连线、
 * 详情侧栏、选区追问、折叠子树全部保留）。它们的宿主原本是 DeepSeek Harness，数据面有两类：
 *
 *   1. REST        `api('/synapse/api/workspaces' | '.../threads/{id}' | '.../branch')`
 *   2. postMessage `dshRpc('synapse:create-session' | 'send-message' | 'fork-session')`
 *
 * 本文件在 `app.js` **之前**加载，用最小侵入的方式把这两类调用改接到 Chiron：
 *   - 包一层 `window.fetch`：`/synapse/api/*` → `/v1/session-map/*`，并把 Chiron 的
 *     `{success,data}` 响应反向转成它期望的 `{workspaces|workspace|thread}` 形状；
 *   - 覆盖 `dshRpc`：三个会话操作映射到 Chiron 的 REST（`fork-session` → 我们新加的
 *     `POST /v1/conversations/{id}/fork`，语义完全对应：`atSeq` → `from_index`）；
 *   - `post` 保持原样（`window.parent.postMessage`）—— 由 Vue 宿主监听并切会话。
 *
 * 为什么能这么接：
 *   - **同源 iframe + httpOnly cookie**：Chiron 的鉴权靠 cookie，fetch 自动携带，
 *     不需要在 iframe 里搬 token；
 *   - **消息不用投影**：dsh-synapse 需要投影是因为 DSH 的会话是文件 log；我们的
 *     `messages` 本来就在 PG 里，`thread` 就等于 `session`，直接读既有接口即可。
 *
 * 已知取舍：读一个工作区时会对每个节点补拉一次会话消息（N+1）。节点量在几十级，
 * 可接受；要优化就在后端加 `?with_messages=1` 一次带回来。
 */
(function () {
  'use strict'

  var API = '/v1/session-map'
  var CONV = '/v1/conversations'

  /** Chiron 的 APIResponse 外壳 → 内层 data（失败时抛错，交给 app.js 的提示路径） */
  function unwrap(body) {
    if (body && body.success === false) throw new Error(body.error || '请求失败')
    return body && Object.prototype.hasOwnProperty.call(body, 'data') ? body.data : body
  }

  function jsonFetch(url, options) {
    options = options || {}
    return fetch(url, {
      credentials: 'include',
      headers: { 'content-type': 'application/json', ...(options.headers || {}) },
      ...options,
    }).then(function (r) { return r.json() })
  }

  // ── 形状映射：Chiron node ↔ dsh-synapse thread ──
  // Chiron：{id, session_id, parent_node_id, edge_kind, x, y, title, collapsed, hidden, branch_from_seq}
  // dsh-synapse：{id, title, dshSessionId, parentId, position:{x,y}, collapsed, archived, messages[]}
  function nodeToThread(node, messages) {
    return {
      id: node.id,
      title: node.title || '',
      dshSessionTitle: node.title || '',
      dshSessionId: node.session_id || '',
      parentId: node.parent_node_id || '',
      // P1-6：分叉锚点的**真实**长度。app.js 的 conversationCards 用
      // `sourceThread?.sourceSeedLength ?? firstChildQuestion?.sourceSeq` 判断这条分支
      // 从父会话的第几条消息长出来（app.js:763）；不给它就只能按索引猜，连线起点会偏。
      // branch_from_seq 正是 fork 时复制的消息条数。
      sourceSeedLength: node.branch_from_seq || undefined,
      position: { x: node.x || 0, y: node.y || 0 },
      collapsed: !!node.collapsed,
      archived: !!node.hidden,
      branchFromSeq: node.branch_from_seq || 0,
      color: node.color || '',
      messages: messages || [],
      pendingProcess: [],
    }
  }

  function threadToNode(thread, workspaceId) {
    return {
      id: thread.id,
      session_id: thread.dshSessionId || '',
      parent_node_id: thread.parentId || '',
      edge_kind: thread.parentId ? 'branch' : 'manual',
      x: Math.round((thread.position && thread.position.x) || 0),
      y: Math.round((thread.position && thread.position.y) || 0),
      title: thread.title || '',
      color: thread.color || '',
      collapsed: !!thread.collapsed,
      hidden: !!thread.archived,
      workspace_id: workspaceId,
    }
  }

  /** 拉一个会话的消息，转成 app.js 期望的 {kind,text,at,sourceSeq} 形状 */
  // ── 消息缓存（P0-4）──
  //
  // 每次 GET 工作区都会对**每个节点**拉一次消息；地图反复刷新时这会把 N+1 放大成
  // N×刷新次数。这里缓存的是 **Promise** 而不是结果 —— 除了省请求，还能把并发
  // 发起的同一个会话合并成一次（后到的直接复用前一个的 Promise）。
  // 失效点：会话有新内容时（live-reply），只失效它自己。
  var messagesCache = new Map()

  function loadMessages(sessionId) {
    if (!sessionId) return Promise.resolve([])
    var hit = messagesCache.get(sessionId)
    if (hit) return hit
    var pending = jsonFetch(CONV + '/' + encodeURIComponent(sessionId) + '?limit=200')
      .then(unwrap)
      .then(function (data) {
        var list = (data && (data.messages || data)) || []
        if (!Array.isArray(list)) return []
        return list
          .filter(function (m) { return m && typeof m.content === 'string' && m.content })
          .map(function (m, i) {
            return {
              id: m.id || 'm' + i,
              kind: m.role === 'user' ? 'user' : 'assistant',
              text: m.content,
              at: m.created_at,
              sourceSeq: i + 1,
            }
          })
      })
      .catch(function () { messagesCache.delete(sessionId); return [] })
    // 缓存 Promise：并发的同一会话只发一次请求
    messagesCache.set(sessionId, pending)
    return pending
  }

  // ── 卡片 = 一个会话（准则 6：同一会话在画布内只出现一次）──
  //
  // 上游的卡片是"一轮对话"（question + answer），一个会话会摊成很多张。
  // Chiron 的语义是**一会话一卡**，卡片显示会话的**标题 + 摘要**。
  // 做法：这里只产出两条合成消息，conversationCards 便恰好生成 1 张卡 ——
  // 于是不必改 app.js 的卡片模板（几万字符的超长行，改动风险高）。
  //
  // 代价：会话详情视图也只看到这两条。这符合准则 2 —— 地图是会话的**投影/引用**，
  // 要看完整对话去对话页。
  var SUMMARY_MAX = 140

  /** 取最近一条助手回复压成单行摘要（折叠空白、超长截断） */
  function summarizeSession(list) {
    for (var i = list.length - 1; i >= 0; i--) {
      var m = list[i]
      if (m && m.role === 'assistant' && typeof m.content === 'string' && m.content.trim()) {
        var t = m.content.trim().replace(/\s+/g, ' ')
        return t.length > SUMMARY_MAX ? t.slice(0, SUMMARY_MAX) + '…' : t
      }
    }
    return ''
  }

  /** 合成"标题 + 摘要"两条消息：让卡片恰好一张，且标题是会话标题 */
  function cardMessages(node, list) {
    var title = node.title || '(未命名会话)'
    var summary = summarizeSession(list)
    var at = list.length ? list[list.length - 1].created_at : undefined
    var out = [{ kind: 'user', text: title, at: at, sourceSeq: 1 }]
    if (summary) out.push({ kind: 'assistant', text: summary, at: at, sourceSeq: 2 })
    return out
  }
  /** 把节点数组组装成 app.js 期望的工作区形状（含 messages） */
  function hydrateNodes(workspaceId, nodes, name, viewport) {
    return Promise.all((nodes || []).map(function (n) {
      // 卡片内容 = 标题 + 摘要（一会话一卡）；完整消息只用于生成摘要，不进卡片
      return loadMessages(n.session_id).then(function (list) { return nodeToThread(n, cardMessages(n, list)) })
    })).then(function (threads) {
      return {
        id: workspaceId,
        title: name || '',
        kind: 'dsh',
        cwd: '',
        threads: threads,
        viewport: viewport || {},
      }
    })
  }

  // ── 一次性迁移：旧版 localStorage → 新版服务端 ──
  //
  // 旧版地图（`components/chat/useSessionMap.ts`）把布局存在浏览器：
  // `localStorage['chiron.sessionMap.v2']` = `{nodes, groups, camera}`，节点是**自由摆放**的
  // 卡片 —— 其中没有 sessionId 的那些就是用户手写的**便签/笔记**。
  // 新版把布局放到服务端（Redis 热层 → 异步落 PG），所以这里做一次搬运：
  // 便签内容、坐标、隐藏状态、分组名都带过去；搬完打标记，避免每次打开都重放。
  var LEGACY_KEY = 'chiron.sessionMap.v2'

  function migrateLegacy(workspaceId) {
    var raw = null
    try { raw = localStorage.getItem(LEGACY_KEY) } catch (e) { return null }
    if (!raw) return null
    var data = null
    try { data = JSON.parse(raw) } catch (e) { return null }   // 损坏数据按"没有"处理
    var legacyNodes = (data && data.nodes) || []
    if (!legacyNodes.length) return null

    var groupNames = {}
    ;((data && data.groups) || []).forEach(function (g) { groupNames[g.id] = g.name || '' })

    var nodes = legacyNodes.map(function (n, i) {
      var isNote = !n.sessionId
      return {
        id: n.id || ('n_legacy_' + i),
        session_id: n.sessionId || '',
        // 旧版没有分支概念，全部按手动摆放接；后续用户分叉才会产生 branch 边
        parent_node_id: '',
        edge_kind: 'manual',
        x: Math.round(n.x || 0),
        y: Math.round(n.y || 0),
        // 便签标题就是它的内容；分组名作为兜底标题，避免搬过来变成无字卡片
        title: n.title || (isNote ? (groupNames[n.groupId] || '便签') : ''),
        hidden: !!n.hidden,
        pinned: !!n.pinned,
      }
    })
    return { id: workspaceId, name: '我的地图', viewport: (data && data.camera) || {}, nodes: nodes }
  }

  // ── 没有历史布局时：把最近会话铺成卡片 ──
  //
  // 否则地图打开就是一片空白 —— 用户没有"从哪开始"的抓手。
  // 按 5 列网格铺开，与 dsh-synapse 的卡片尺寸（310×276 + 间距）保持一致的手感。
  function importRecentSessions(workspaceId) {
    return jsonFetch(CONV + '?per_page=30').then(unwrap).then(function (list) {
      if (!Array.isArray(list) || !list.length) return null
      var nodes = list.map(function (s, i) {
        return {
          id: 'n_' + s.id,
          session_id: s.id,
          parent_node_id: '',
          edge_kind: 'manual',
          x: (i % 5) * 360,
          y: Math.floor(i / 5) * 340,
          title: '',
          hidden: false,
        }
      })
      return { id: workspaceId, name: '我的地图', viewport: {}, nodes: nodes }
    })
  }

  /** 把工作区补齐成 app.js 期望的形状；空画布时先做迁移/导入再补齐 */
  function hydrateWorkspace(ws) {
    currentWorkspaceId = ws.id
    var nodes = ws.nodes || []
    if (nodes.length) return hydrateNodes(ws.id, nodes, ws.name, ws.viewport)

    var seeded = migrateLegacy(ws.id)
    var pending = seeded ? Promise.resolve(seeded) : importRecentSessions(ws.id)
    return pending.then(function (payload) {
      if (!payload) return hydrateNodes(ws.id, [], ws.name, ws.viewport)
      // 落库（走 PUT：Redis 热层立即生效，PG 由后台 flusher 异步写）
      return jsonFetch(API + '/workspaces/' + encodeURIComponent(ws.id), {
        method: 'PUT',
        body: JSON.stringify(payload),
      })
        .catch(function () { /* 保存失败也要让用户先看到内容 */ })
        .then(function () { return hydrateNodes(ws.id, payload.nodes, payload.name, payload.viewport) })
    })
  }

  /** 内存快照 + 防抖整体保存（对应后端的"整体保存"设计） */
  var snapshots = {}   // workspaceId → {id,title,threads}
  var saveTimers = {}
  var currentWorkspaceId = null   // 正在看的画布，供"会话跑完自动刷新"用

  function scheduleSave(workspaceId) {
    if (saveTimers[workspaceId]) clearTimeout(saveTimers[workspaceId])
    saveTimers[workspaceId] = setTimeout(function () {
      delete saveTimers[workspaceId]
      var snap = snapshots[workspaceId]
      if (!snap) return
      var body = {
        id: workspaceId,
        name: snap.title || '',
        viewport: snap.viewport || {},
        nodes: (snap.threads || []).map(function (t) { return threadToNode(t, workspaceId) }),
      }
      jsonFetch(API + '/workspaces/' + encodeURIComponent(workspaceId), {
        method: 'PUT',
        body: JSON.stringify(body),
      }).catch(function () { /* 下一次拖动会再存一次 */ })
    }, 800)  // 与 dsh-synapse 的服务端防抖同量级（index.js 的 SAVE_DEBOUNCE_MS=800）
  }

  function snapshot(workspaceId) {
    return snapshots[workspaceId]
  }

  // ── 1. fetch 劫持：REST 形状映射 ──
  var origFetch = window.fetch.bind(window)
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || ''
    if (url.indexOf('/synapse/api/') === -1) return origFetch(input, init)
    var method = ((init && init.method) || 'GET').toUpperCase()
    var body = init && init.body ? JSON.parse(init.body) : {}

    function respond(payload, status) {
      return Promise.resolve(new Response(JSON.stringify(payload), {
        status: status || 200,
        headers: { 'content-type': 'application/json' },
      }))
    }

    // 列表：GET /synapse/api/workspaces
    if (url.indexOf('/synapse/api/workspaces') === 0 && url.indexOf('/synapse/api/workspaces/') === -1) {
      if (method === 'GET') {
        return jsonFetch(API + '/workspaces').then(unwrap).then(function (d) {
          var list = (d && d.workspaces) || []
          // 没有画布就建一个默认的：让"第一次打开就是可用状态"
          if (!list.length) {
            return jsonFetch(API + '/workspaces', { method: 'POST', body: JSON.stringify({ name: '我的地图' }) })
              .then(unwrap)
              .then(function (ws) { return { workspaces: [{ id: ws.id, title: ws.name || '我的地图' }] } })
          }
          return { workspaces: list.map(function (w) { return { id: w.id, title: w.name || '未命名' } }) }
        }).then(function (p) { return respond(p) })
      }
      if (method === 'POST') {
        return jsonFetch(API + '/workspaces', {
          method: 'POST',
          body: JSON.stringify({ name: body.title || body.name || '我的地图' }),
        }).then(unwrap).then(function (ws) {
          return respond({ workspace: { id: ws.id, title: ws.name || '', threads: [] } }, 201)
        })
      }
    }

    // 详情/保存/删除：/synapse/api/workspaces/{id}
    var wsMatch = /\/synapse\/api\/workspaces\/([^/?]+)/.exec(url)
    if (wsMatch) {
      var wsId = decodeURIComponent(wsMatch[1])
      if (method === 'GET') {
        return jsonFetch(API + '/workspaces/' + encodeURIComponent(wsId))
          .then(unwrap)
          .then(hydrateWorkspace)
          .then(function (ws) {
            snapshots[wsId] = ws
            return respond({ workspace: ws })
          })
      }
      if (method === 'PATCH' || method === 'PUT') {
        var snap = snapshot(wsId) || { id: wsId, threads: [] }
        if (body.title !== undefined) snap.title = body.title
        if (body.viewport !== undefined) snap.viewport = body.viewport
        scheduleSave(wsId)
        return respond({ workspace: snap })
      }
      if (method === 'DELETE') {
        return jsonFetch(API + '/workspaces/' + encodeURIComponent(wsId), { method: 'DELETE' })
          .then(function () { delete snapshots[wsId]; return respond({ removed: true }) })
      }
    }

    // 线程（节点）：PATCH/DELETE 改内存 + 防抖整体保存；branch 直接打 Chiron 的 fork 端点
    var thMatch = /\/synapse\/api\/threads\/([^/?]+)(\/branch)?/.exec(url)
    if (thMatch) {
      var thId = decodeURIComponent(thMatch[1])
      var isBranch = !!thMatch[2]
      var wsForThread = null
      Object.keys(snapshots).some(function (k) {
        var t = (snapshots[k].threads || []).filter(function (x) { return x.id === thId })[0]
        if (t) { wsForThread = k; return true }
        return false
      })
      var hostWs = wsForThread || Object.keys(snapshots)[0]
      var hostSnap = hostWs ? snapshot(hostWs) : null
      var hostThread = hostSnap ? (hostSnap.threads || []).filter(function (x) { return x.id === thId })[0] : null

      if (isBranch && hostThread && hostThread.dshSessionId) {
        // atSeq → from_index：我们的 fork 语义是"保留前 N 条（含第 N 条）"
        return jsonFetch(CONV + '/' + encodeURIComponent(hostThread.dshSessionId) + '/fork', {
          method: 'POST',
          body: JSON.stringify({ from_index: body.atSeq || 1, title: body.title || '' }),
        }).then(unwrap).then(function (r) {
          var child = {
            id: 'n_' + r.session_id,
            title: body.title || '分支',
            dshSessionId: r.session_id,
            parentId: thId,
            position: { x: (hostThread.position.x || 0) + 360, y: (hostThread.position.y || 0) + 300 },
            messages: [],
            pendingProcess: [],
          }
          if (hostSnap) {
            hostSnap.threads.push(child)
            snapshots[hostWs] = hostSnap
            scheduleSave(hostWs)
          }
          return respond({ thread: child }, 201)
        })
      }
      if (hostSnap && !hostThread && method === 'PATCH') {
        // 未知节点 = 新建（便签创建走这条路）：补进内存快照并触发整体保存
        var created = Object.assign({ id: thId, title: '', dshSessionId: '', parentId: '', position: { x: 0, y: 0 }, messages: [], pendingProcess: [] }, body)
        hostSnap.threads.push(created)
        snapshots[hostWs] = hostSnap
        scheduleSave(hostWs)
        return respond({ thread: created })
      }
      if (hostSnap && hostThread) {
        if (method === 'PATCH') {
          if (body.position) hostThread.position = body.position
          if (body.title !== undefined) hostThread.title = body.title
          if (body.collapsed !== undefined) hostThread.collapsed = body.collapsed
          if (body.archived !== undefined) hostThread.archived = body.archived
          scheduleSave(hostWs)
          return respond({ thread: hostThread })
        }
        if (method === 'DELETE') {
          // 准则 4：画布内删除**默认只移出画布**（准则 2：节点是会话的引用，删引用不删数据）。
          // 只有显式带 with_session=1 时才连真实会话记录一起删 —— 由 UI 的"同时删除会话记录"
          // 选项决定，默认不勾选。
          if (url.indexOf('with_session=1') !== -1 && hostThread.dshSessionId) {
            var sessionToDelete = hostThread.dshSessionId
            void jsonFetch(CONV + '/' + encodeURIComponent(sessionToDelete), { method: 'DELETE' })
              .catch(function () { /* 会话删失败不回滚节点：至少画布已按用户意愿清干净 */ })
            messagesCache.delete(sessionToDelete)
          }
          hostSnap.threads = hostSnap.threads.filter(function (t) { return t.id !== thId })
          snapshots[hostWs] = hostSnap
          scheduleSave(hostWs)
          return respond({ removed: true })
        }
      }
      return respond({ error: 'thread not found' }, 404)
    }

    // 会话同步（它有这个端点）：给一个空实现，避免整页报错
    if (url.indexOf('/synapse/api/sessions/sync') !== -1) return respond({ workspaces: [] })

    return respond({ error: 'not mapped' }, 404)
  }

  // ── 2. dshRpc 覆盖：会话操作 → Chiron REST ──
  // ── 地图侧自行订阅事件流（P0-1 修复）──
  //
  // 为什么必须自己订阅：`/v1/events` 的 `session_id` 是**必填**（后端 P0-S5 防止订到全站
  // 事件流、泄露他人对话），一次只能订一个会话；而宿主 ChatView 订的是**它自己的**当前
  // 会话 —— 地图里正在跑的那个会话它根本不知道，所以永远收不到增量。
  //
  // 收到增量后用 postMessage 发回**本窗口**：app.js 的监听只校验 origin + source，
  // 自己发给自己同样满足，于是它按既有的 live-reply 协议就地 patch 卡片。
  var streamSources = new Map()   // sessionId -> EventSource
  var streamText = new Map()      // sessionId -> 累积文本

  function newUuid() {
    return (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : 'cmid-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2)
  }
  /**
   * 把一个会话摆进当前画布（若还没摆）。
   *
   * 地图是"对话页面的**另一种实现**"，不是宿主视图的附属：它可能自己新建会话
   * （synapse:create-session）、也可能从宿主那边切过来（synapse:current-session）。
   * 这两种情况都必须让卡片真的出现在图上，否则用户新建完会话却在地图里找不到它。
   *
   * 取舍：用户手动删掉的节点会被重新摆回（因为我们只按"有没有"判断）。
   * "新会话看不见"比"删掉的又冒出来"更难受，所以先这样（要彻底解决需记录手动删除集合）。
   */
  function placeSession(session) {
    if (!session || !session.id || !currentWorkspaceId) return
    var snap = snapshots[currentWorkspaceId]
    if (!snap) return
    var mine = snap.threads || (snap.threads = [])
    if (mine.some(function (t) { return t.dshSessionId === session.id })) return
    var bottom = mine.reduce(function (max, t) { return Math.max(max, (t.position && t.position.y) || 0) }, 0)
    mine.push({
      id: 'n_' + session.id,
      title: session.title || '',
      dshSessionId: session.id,
      parentId: '',
      position: { x: 86, y: bottom + 320 },
      messages: [],
      pendingProcess: [],
    })
    snapshots[currentWorkspaceId] = snap
    scheduleSave(currentWorkspaceId)
    if (typeof window.openWorkspace === 'function') {
      window.openWorkspace(currentWorkspaceId, { preserveCanvasCamera: true })
    }
  }
  function selfPost(payload) {
    window.postMessage({ source: 'chiron-sessionmap', ...payload }, window.location.origin)
  }

  function watchSessionStream(sessionId) {
    if (!sessionId || streamSources.has(sessionId)) return
    var es = new EventSource('/v1/events?session_id=' + encodeURIComponent(sessionId), { withCredentials: true })
    streamSources.set(sessionId, es)
    es.onmessage = function (event) {
      var d = null
      try { d = JSON.parse(event.data) } catch (_) { return }
      if (!d || typeof d.type !== 'string') return
      if (d.type === 'text') {
        var chunk = (d.data && d.data.content) || ''
        if (!chunk) return
        var acc = (streamText.get(sessionId) || '') + chunk
        streamText.set(sessionId, acc)
        selfPost({ type: 'synapse:live-reply', sessionId: sessionId, running: true, text: acc })
        return
      }
      if (d.type === 'turn_done' || d.type === 'error') {
        streamText.delete(sessionId)
        selfPost({ type: 'synapse:live-reply', sessionId: sessionId, running: false })
        messagesCache.delete(sessionId)
        // 回合结束：重拉当前画布，把已经落库的完整消息带出来
        if (currentWorkspaceId && typeof window.openWorkspace === 'function') {
          window.openWorkspace(currentWorkspaceId, { preserveCanvasCamera: true })
        }
      }
    }
    // 断线交给浏览器原生自动重连（会带 Last-Event-ID，服务端从缓冲补发）
    es.onerror = function () { /* noop */ }
  }

  window.dshRpc = function (type, payload) {
    payload = payload || {}
    if (type === 'synapse:create-session') {
      return jsonFetch(CONV, { method: 'POST', body: JSON.stringify({ title: payload.title || '新对话' }) })
        .then(unwrap)
        .then(function (session) {
          // 地图是"对话页面的另一种实现"，不是宿主视图的附属：它自己建的会话必须自己摆进画布，
          // 不能等宿主推 current-session —— 宿主根本不知道这次创建，结果就是"新建了会话但地图上没有"。
          placeSession(session)
          return session
        })
    }
    if (type === 'synapse:send-message') {
      watchSessionStream(payload.sessionId)
      return jsonFetch('/v1/agent/submit', {
        method: 'POST',
        body: JSON.stringify({
          session_id: payload.sessionId,
          // 追问走**特定格式**的提示词：方括号标记 + 包裹原文，让模型明确"只要一句话"，
          // 也方便将来在服务端按格式识别这类一次性问答（普通对话与追问得以区分）。
          content: payload.mode === 'followup'
            ? '（【请简短回答问题】:(' + (payload.text || '') + ')）'
            : (payload.text || ''),
          context: {},
          // 幂等键：服务端用 Redis SETNX 做 5 分钟去重（见 internal/api/submit_handler.go）。
          // **必须由客户端生成** —— 只有它知道"这两次提交是同一条消息"（网络重试/重发时这个 ID 不变）。
          // 缺了它，请求会被当成重复提交而无效。
          llm_config: { client_msg_id: newUuid() },
        }),
      }).then(function () { return { ok: true } })
    }
    if (type === 'synapse:fork-session') {
      return jsonFetch(CONV + '/' + encodeURIComponent(payload.sessionId) + '/fork', {
        method: 'POST',
        body: JSON.stringify({ from_index: payload.atSeq || 1 }),
      }).then(unwrap)
    }
    return Promise.reject(new Error('未支持的 RPC：' + type))
  }

  // ── 3. 宿主到地图：会话跑完后刷新画布 ──
  //
  // 上游地图靠宿主持续推事件来更新卡片；我们只在"某个会话从运行态结束"时，
  // 让地图重拉一次当前画布（走本文件的 fetch 劫持，重新 hydrate，新消息就落到卡片上）。
  // app.js 的 openWorkspace 是脚本顶层的函数声明（因此挂在 window 上），可以直接调用。
  window.addEventListener('message', function (e) {
    if (e.origin !== window.location.origin) return
    var d = e.data
    if (!d || d.source !== 'chiron-sessionmap') return
    // 宿主切到某会话 → 交给 placeSession 摆进画布（它自带"已存在就跳过"与防抖保存）（P1-5）。
    if (d.type === 'synapse:current-session' && d.session && d.session.id) {
      placeSession(d.session)
    }
    if (d.type === 'synapse:live-reply' && d.running === false && currentWorkspaceId &&
        typeof window.openWorkspace === 'function') {
      // 该会话有新内容：**只**失效它自己的缓存（其余节点继续命中，避免整图重拉）
      messagesCache.delete(d.sessionId)
      window.openWorkspace(currentWorkspaceId, { preserveCanvasCamera: true })
    }
  })
  console.info('[sessionmap] Chiron 缝合层已就绪：REST → /v1/session-map, RPC → /v1/conversations')
})()
