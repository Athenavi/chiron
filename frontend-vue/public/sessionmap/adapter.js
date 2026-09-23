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
 *   - `post` 保持原样（`window.parent.postMessage`）—— 由 Vue 宿主监听并切会话。
 *
 * ⚠️ 第 2 类（会话操作）**不在本文件里**：`dshRpc` 是 app.js 的顶层函数声明，它会覆盖
 * 本文件里的任何同名定义（这里曾写过一份 REST 实现，从未生效，已删除）。因此那三条操作
 * 由**宿主 ChatView** 代理并回执 —— 见
 * `frontend-vue/src/components/chat/sessionmapBridge.ts`。
 *
 * 为什么能这么接：
 *   - **同源 iframe + httpOnly cookie**：Chiron 的鉴权靠 cookie，fetch 自动携带，
 *     不需要在 iframe 里搬 token；
 *   - **消息不用投影**：dsh-synapse 需要投影是因为 DSH 的会话是文件 log；我们的
 *     `messages` 本来就在 PG 里，`thread` 就等于 `session`，直接读既有接口即可。
 *
 * 已知取舍：
 *   - 读一个工作区时会对每个节点补拉一次会话消息（N+1）。节点量在几十级，
 *     可接受；要优化就在后端加 `?with_messages=1` 一次带回来。
 *   - 卡片只投影「主卡（标题 + 最新回复）+ 追问对」两种内容，不是完整对话 ——
 *     完整对话去对话页看（地图是会话的投影/引用）。
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
  function nodeToThread(node, messages, meta) {
    meta = meta || {}
    // 展示名：别名 → 地图节点标题 → 会话真实标题。
    // 用户在地图上改过的节点标题优先；没改过时用**会话的别名/标题**（而不是空字符串），
    // 这样"刚摆上画布"的会话也不会显示成占位标题。
    var alias = (meta.alias || '').trim()
    var displayName = alias || node.title || meta.title || ''
    return {
      id: node.id,
      title: displayName,
      dshSessionTitle: displayName,
      // 徽标数据：卡片要显示会话标签与别名（用户要求）。原样带给 app.js 的补丁区块。
      tag: (meta.tag || '').trim(),
      alias: alias,
      realTitle: meta.title || '',
      // 分支血缘：父会话展示名 + 分叉点（app.js 补丁渲染"分支·自 X"）
      parentTitle: meta.parentTitle || '',
      branchFromSeq: meta.branchFromSeq || 0,
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

  /**
   * 会话级元数据（别名 / 标签 / 真实标题）—— 与消息**同一个响应**里拿到，零额外请求。
   *
   * 为什么要缓存：卡片要显示会话标签与别名（用户要求），而这两者只在会话对象上
   * （`GET /v1/conversations/{id}` 返回 `{title, tag, alias, messages}`）。
   * 单独再发一次请求会让本已存在的 N+1 翻倍。
   */
  var sessionMeta = new Map()

  function loadMessages(sessionId) {
    if (!sessionId) return Promise.resolve([])
    var hit = messagesCache.get(sessionId)
    if (hit) return hit
    var pending = jsonFetch(CONV + '/' + encodeURIComponent(sessionId) + '?limit=200')
      .then(unwrap)
      .then(function (data) {
        var list = (data && (data.messages || data)) || []
        if (data && typeof data === 'object') {
          sessionMeta.set(sessionId, {
            title: typeof data.title === 'string' ? data.title : '',
            tag: typeof data.tag === 'string' ? data.tag : '',
            alias: typeof data.alias === 'string' ? data.alias : '',
            // 分支血缘：会话列表与地图卡片都要显示"这是分支、分支自谁、从第几条分叉"
            parentTitle: typeof data.parent_title === 'string' ? data.parent_title : '',
            branchFromSeq: Number.isInteger(data.branch_from_seq) ? data.branch_from_seq : 0,
          })
        }
        if (!Array.isArray(list)) return []
        return list
          .filter(function (m) { return m && typeof m.content === 'string' && m.content })
          .map(function (m, i) {
            return {
              id: m.id || 'm' + i,
              kind: m.role === 'user' ? 'user' : 'assistant',
              // 原始 role 也要留着：追问卡投影需要区分"真正的助手回答"与 tool 消息
              // （app.js 只认 kind，多一个字段对它无害）
              role: m.role,
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
  // Chiron 的语义是**一会话一卡**：卡片显示会话的**标题 + 摘要**。
  // 做法：合成两条消息，conversationCards 便恰好生成一张主卡。
  //
  // 例外（用户要求）：**追问**必须保留成卡片。追问是一次性问答，此前不落任何地图数据，
  // 只在 `state.pendingReplies` 里有个临时态 —— 回合结束即消失，用户看不到自己问过什么、
  // 答了什么（"追问卡片完成后永久丢失"）。现在把它投影成主卡之后的追加卡片，见
  // followupMessages：数据源是**会话消息**，所以会话删除 ⇒ 卡片随之消失（要求的生命周期）。
  //
  // 详情视图仍只看到这几条（含追问对）—— 这一条留待"详情改用真实消息"那一步。
  var SUMMARY_MAX = 140
  /** 追问卡在天花板上的截断长度：卡片只需"一眼可读"，完整内容去对话页看 */
  var FOLLOWUP_MAX = 400

  /** 取最近一条助手回复压成单行摘要（折叠空白、超长截断） */
  function summarizeSession(list) {
    for (var i = list.length - 1; i >= 0; i--) {
      var m = list[i]
      if (!m || m.role !== 'assistant') continue
      var text = messageText(m)
      if (text.trim()) {
        var t = text.trim().replace(/\s+/g, ' ')
        return t.length > SUMMARY_MAX ? t.slice(0, SUMMARY_MAX) + '…' : t
      }
    }
    return ''
  }

  /**
   * 取一条消息的正文。
   *
   * ⚠️ 必须同时认 `text` 与 `content`：本文件读会话消息时会把 `content` 映射成 `text`
   * （见 loadMessages），而 cardMessages 的入参正是**映射后**的列表 —— 此前
   * summarizeSession 只读 `content`，于是**主卡的摘要永远是空的**（卡片上只剩标题）。
   * 统一走这个取值函数，形状变化不会再让摘要静默消失。
   */
  function messageText(m) {
    if (!m) return ''
    if (typeof m.text === 'string') return m.text
    if (typeof m.content === 'string') return m.content
    return ''
  }

  /**
   * 追问的提示词格式 —— 与宿主（`sessionmapBridge.ts` 的 `FOLLOWUP_TEMPLATE`）成对演进。
   *
   * ⚠️ 外层全角括号是**可选**的：历史上 adapter 自己发消息时用的是带括号的写法
   * （`（【请简短回答问题】:(…)）`），现在由宿主发送、不带括号。库里两种格式都可能有，
   * 正则必须都认 —— 否则"追问卡"这种**历史数据**永远投影不出来（功能等于没做）。
   */
  var FOLLOWUP_RE = /^（?【请简短回答问题】:\(([\s\S]*)\)）?$/

  function followupQuestion(text) {
    if (typeof text !== 'string') return null
    var matched = FOLLOWUP_RE.exec(text.trim())
    return matched ? matched[1] : null
  }

  function clipText(text, max) {
    var t = String(text === null || text === undefined ? '' : text).trim()
    return t.length > max ? t.slice(0, max) + '…' : t
  }

  /**
   * 把会话里的**追问对**（问题 + 回答）投影成追加卡片。
   *
   * 为什么不需要新表、也不需要改 app.js：
   *   - 追问的问题与回答**本来就在会话消息里**（宿主走的是与对话页同一条 `/submit` 落库）；
   *   - app.js 的切分规则是"**每条 user 消息一张卡**"（app.js:693），同一 thread 的多张卡
   *     会自动串成链（追问卡挂在主卡之后，app.js:764）—— 正好是要的形态。
   *
   * 生命周期：数据源是会话消息 ⇒ 会话删除、消息消失 ⇒ 追问卡随之消失（正是要求的行为），
   * 不需要额外的清理逻辑，也不会留下孤儿数据。
   */
  function followupMessages(list) {
    var out = []
    for (var i = 0; i < list.length; i++) {
      var m = list[i]
      if (!m || m.role !== 'user') continue
      var question = followupQuestion(messageText(m))
      if (question === null) continue
      // 回答 = 该追问之后、下一条 user 消息之前的**最后一条真正的助手消息**
      // （tool 消息不参与 —— 否则卡片上的"回答"可能是工具回显）
      var answer = ''
      for (var j = i + 1; j < list.length; j++) {
        var reply = list[j]
        if (!reply || reply.role === 'user') break
        if (reply.role === 'assistant' && messageText(reply).trim()) {
          answer = messageText(reply)
        }
      }
      // sourceSeq 沿用原消息序号：app.js 用它做卡片 id（`thread:turn:<seq>`），
      // 于是**同一轮追问的卡片 id 稳定** —— 拖动位置（cardPositions）能持久化。
      out.push({ kind: 'user', text: clipText(question, FOLLOWUP_MAX), at: m.at, sourceSeq: m.sourceSeq })
      if (answer) out.push({ kind: 'assistant', text: clipText(answer, FOLLOWUP_MAX), at: m.at, sourceSeq: m.sourceSeq })
    }
    return out
  }

  /** 合成"标题 + 摘要"主卡消息，再追加投影出来的追问卡 */
  function cardMessages(node, list) {
    var title = node.title || '(未命名会话)'
    var summary = summarizeSession(list)
    var at = list.length ? list[list.length - 1].created_at : undefined
    var out = [{ kind: 'user', text: title, at: at, sourceSeq: 1 }]
    if (summary) out.push({ kind: 'assistant', text: summary, at: at, sourceSeq: 2 })
    return out.concat(followupMessages(list))
  }
  /** 把节点数组组装成 app.js 期望的工作区形状（含 messages） */
  function hydrateNodes(workspaceId, nodes, name, viewport) {
    return Promise.all((nodes || []).map(function (n) {
      // 卡片内容 = 主卡（标题 + 最新回复）+ 追问卡（会话里的追问对）；
      // 完整消息只用于生成这两者，其余不进卡片（要看完整对话去对话页）
      return loadMessages(n.session_id).then(function (list) {
        return nodeToThread(n, cardMessages(n, list), sessionMeta.get(n.session_id) || {})
      })
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

  // 迁移**只做一次**。没有这个标记时，只要画布被清空（例：删掉所有会话、
  // 或清掉热层），下一次打开就会把旧版 localStorage 里的节点重新灌回来 ——
  // 表现为"删掉的会话反复出现"（本项目实际踩过）。
  var MIGRATED_FLAG = 'chiron:sessionmap:migrated'

  function migrateLegacy(workspaceId) {
    try {
      if (localStorage.getItem(MIGRATED_FLAG)) return null
    } catch (e) {
      return null // 读不到 localStorage 就不迁移，避免行为不确定
    }
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
        .then(function () {
          // 落库成功才算迁移完成：失败不打标记，下次还能重试
          try {
            localStorage.setItem(MIGRATED_FLAG, '1')
            // 旧键用完即删：留着它等于留一个永久污染源（清空画布就会被重新灌回）
            localStorage.removeItem(LEGACY_KEY)
          } catch (e) { /* 隐私模式忽略 */ }
        })
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
          // ── 唯一画布 ──
          // 上游的"工作区/多画布"是它的宿主模型；Chiron 里地图是会话的**投影**，
          // 不需要多份布局 —— 每个用户只认一张（后端 ListWorkspaces 已按 updated_at DESC
          // 排序，取第一个即最近使用的那张）。后端仍保留多画布能力，将来要"多视图"不必改数据层。
          if (list.length > 1) list = [list[0]]
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

      if (isBranch) {
        // 分支是**两段式**的：会话 fork 由 app.js 经宿主 RPC 完成（`synapse:fork-session`），
        // 这里只负责登记地图节点。
        //
        // 此前这里又 fork 了一次（还拿 `body.atSeq || 1` 当分叉点），于是"节点挂的会话"
        // 和"随后真正发消息的会话"是两个不同的会话：卡片永远没有回复 ——
        // 用户看到的现象就是"无法创建分支"。
        var branchedSessionId = body.dshSessionId || ''
        if (branchedSessionId) {
          var childFromHost = {
            id: 'n_' + branchedSessionId,
            title: body.title || '分支',
            dshSessionId: branchedSessionId,
            parentId: thId,
            // 位置由 app.js 算好（它知道草稿卡片的落点）；缺省时退回父节点右下角
            position: body.position || {
              x: (hostThread ? hostThread.position.x : 0) + 360,
              y: (hostThread ? hostThread.position.y : 0) + 300,
            },
            // 分叉点：fork 复制的消息条数，卡片摘要与血缘提示都要它
            branchFromSeq: body.atSeq || 0,
            messages: [],
            pendingProcess: [],
          }
          if (hostSnap) {
            hostSnap.threads.push(childFromHost)
            snapshots[hostWs] = hostSnap
            scheduleSave(hostWs)
          }
          return respond({ thread: childFromHost }, 201)
        }
        // 兼容没带会话 id 的旧调用方：维持原来的"由适配层自己 fork"行为
        if (hostThread && hostThread.dshSessionId) {
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

  // ── 2. 会话操作（新建 / 发消息 / 分叉）：由**宿主**代理，适配层不参与 ──
  //
  // app.js 用 `dshRpc(type, payload)` 发起这三类操作：它 postMessage 给父窗口并等一条
  // 带 requestId 的回执（app.js:142 定义、app.js:2070 结算、20 秒超时）。
  // **app.js 自己定义了顶层函数 `dshRpc`，会把这里曾经写过的一份 REST 实现覆盖掉** ——
  // 那份代码从未生效过（已删除，避免后人以为它在工作）。
  //
  // 现在由宿主 ChatView 接管并回执，编排逻辑见
  // `frontend-vue/src/components/chat/sessionmapBridge.ts`。
  //
  // 附带好处：会话的主人始终是宿主 —— 地图新建的会话会出现在会话列表里，
  // 地图发出的消息也走宿主同一条 /submit 链路（幂等键、SSE 转发、运行态都一致）。
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
  function placeSession(session, skipReload) {
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
    // 批量导入时由调用方统一重拉一次，避免 N 次 openWorkspace
    if (!skipReload && typeof window.openWorkspace === 'function') {
      window.openWorkspace(currentWorkspaceId, { preserveCanvasCamera: true })
    }
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
  /**
   * 把"会话列表里还不在地图上的会话"批量导入当前画布。
   *
   * 为什么是导入而不是创建：会话地图是会话的**投影/引用**（准则 2），不是创建源。
   * 新建会话在对话页做，地图只负责把已存在的会话摆上来。
   */
  /**
   * 列出"可以导入到当前画布"的会话（已在画布上的不算 —— 准则 6：画布内不重复）。
   *
   * 给导入面板用：面板只负责渲染与勾选，取数与去重都在这里，
   * 免得 app.js 再实现一遍"哪些已经在地图上了"。
   */
  window.synapseListImportable = function () {
    if (!currentWorkspaceId) return Promise.resolve([])
    var snap = snapshots[currentWorkspaceId]
    var existing = {}
    ;((snap && snap.threads) || []).forEach(function (t) {
      if (t.dshSessionId) existing[t.dshSessionId] = true
    })
    return jsonFetch(CONV + '?per_page=100')
      .then(unwrap)
      .then(function (list) {
        if (!Array.isArray(list)) return []
        return list
          .filter(function (s) { return s && s.id && !existing[s.id] })
          .map(function (s) { return { id: s.id, title: s.title || '(未命名会话)' } })
      })
  }
  window.synapseImportSessions = function (ids) {
    if (!currentWorkspaceId) return Promise.resolve(0)
    return jsonFetch(CONV + '?per_page=100')
      .then(unwrap)
      .then(function (list) {
        if (!Array.isArray(list)) return 0
        var snap = snapshots[currentWorkspaceId]
        var before = (snap && snap.threads ? snap.threads.length : 0)
        // 指定了 ids 就只导入那些；否则导入全部未入图的（向后兼容）
        var want = Array.isArray(ids) && ids.length ? new Set(ids) : null
        list.forEach(function (s) {
          if (!s || !s.id) return
          if (want && !want.has(s.id)) return
          placeSession(s, true)
        })
        var after = (snapshots[currentWorkspaceId] && snapshots[currentWorkspaceId].threads
          ? snapshots[currentWorkspaceId].threads.length : 0)
        // 有新增才重拉（保持相机），并触发一次整体保存
        if (after !== before) {
          scheduleSave(currentWorkspaceId)
          if (typeof window.openWorkspace === 'function') {
            window.openWorkspace(currentWorkspaceId, { preserveCanvasCamera: true })
          }
        }
        return after - before
      })
  }
  // ── 顶部「对话」按钮 → 回到对话页 ──
  //
  // 上游顶栏中央有个「对话 / 会话地图」切换，其中「对话」原本是通知它的宿主切视图的
  // （data-action="close"）。这里接成"告诉 Chiron 宿主关掉地图浮层、回到对话页"，
  // 于是地图自己就能提供返回入口，不必再靠浮层右上角那个关闭按钮。
  // 用捕获阶段监听：先于 app.js 的处理，不与它自己的逻辑打架。
  document.addEventListener('click', function (event) {
    var el = event.target instanceof Element ? event.target.closest('[data-action="close"]') : null
    if (!el) return
    if (window.parent !== window) {
      window.parent.postMessage(
        { source: 'chiron-sessionmap', type: 'synapse:close-map' },
        window.location.origin
      )
    }
  }, true)

  // ── 供回归测试使用的纯函数出口 ──
  //
  // 本文件是**浏览器脚本**（public/ 下的静态资产，不经打包），所以没有模块导出。
  // 这几个函数是"追问卡投影"的数据正确性所在（判定哪些消息是追问、配对回答、截断），
  // 必须有测试盯着 —— 于是显式挂一个只读出口，测试用 jsdom 载入本文件后直接调用。
  window.__chironAdapter = {
    followupQuestion: followupQuestion,
    followupMessages: followupMessages,
    cardMessages: cardMessages,
    clampFollowupText: clipText,
    FOLLOWUP_MAX: FOLLOWUP_MAX,
    /**
     * 失效某个会话的缓存（消息 + 元数据）。
     *
     * 供 app.js 的补丁区块在"重命名 / 备注别名 / 标签"成功后调用：
     * 那三类改动都落在**会话对象**上（`/v1/conversations/{id}`），而地图的卡片标题与徽标
     * 正是从会话对象投影出来的 —— 不清缓存，重拉画布还是旧名字。
     */
    invalidateSession: function (sessionId) {
      if (!sessionId) return
      messagesCache.delete(sessionId)
      sessionMeta.delete(sessionId)
    },
    sessionMetaOf: function (sessionId) { return sessionMeta.get(sessionId) || null },
  }

  console.info('[sessionmap] Chiron 缝合层已就绪：REST → /v1/session-map, RPC → 宿主 ChatView')
})()
