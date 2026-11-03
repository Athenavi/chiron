/**
 * Todo 面板状态 —— **按 session 存放，不进 `items`**。
 *
 * 为什么单独一个 Map 而不是塞进会话运行态：`items`（`runtimes[].items`）是
 * 消息流的投影，切会话、重放历史、`/clear` 都会整体重建它。任务板是**跨轮次
 * 存活的全局状态**，两者的生命周期根本不同：
 *
 * * 放进 `items` → 面板会随消息重建而丢失；反过来，为了让面板不丢而在重建时
 *   特判保留它，又会让"历史重放"产出本该没有的面板（刷新后出现一个上一轮任务
 *   留下的清单，而模型那边早没了）。
 * * 独立 Map → 两边互不干扰：消息区怎么重建都不动面板，面板更新也不触发消息区
 *   重排（这点很关键：一旦面板挂在列表里，每次状态推进都会让虚拟列表的 key 集合
 *   变化，正在流式输出的内容会被迫重新挂载）。
 *
 * 清空时机由调用方显式决定（新回合 / 新会话 / 收到空清单），不做隐式推导 ——
 * 隐式规则正是"面板莫名清空/莫名残留"的来源。
 */

import { computed, ref } from 'vue'

import {
  EMPTY_BOARD,
  projectTodoBoard,
  shouldApply,
  type TodoBoard,
} from '../components/chat/todoProjection'

/** 折叠偏好的存储键（全局，不是按会话：用户的折叠习惯不随会话变）。 */
const COLLAPSED_STORAGE_KEY = 'chiron.todoPanel.collapsed'

export function useTodoPanel() {
  /**
   * 每个会话一块板子。
   *
   * `shallowRef` + 整体替换：板子每次更新都是**新对象**（投影出来的），不需要
   * 深层响应式。用深层 ref 会让每次 chunk 到达时都去代理整个数组，在大清单上
   * 是纯浪费 —— 而这里是热路径（每一轮工具调用都会更新）。
   */
  const boards = ref<Record<string, TodoBoard>>({})

  /** 折叠状态：默认展开（有清单时用户第一反应是看它），用户折叠后记住。 */
  const collapsed = ref<boolean>(readCollapsed())

  function readCollapsed(): boolean {
    try {
      return window.localStorage.getItem(COLLAPSED_STORAGE_KEY) === '1'
    } catch {
      // 隐私模式/存储被禁用时 localStorage 会抛：面板默认展开即可，不该因此崩掉。
      return false
    }
  }

  function toggleCollapsed(): void {
    collapsed.value = !collapsed.value
    try {
      window.localStorage.setItem(COLLAPSED_STORAGE_KEY, collapsed.value ? '1' : '0')
    } catch {
      /* 记不住偏好不影响本次使用 */
    }
  }

  /**
   * 直接放入一份**已投影**的板子（历史恢复用）。
   *
   * 为什么不让调用方把 `board.rows` 再喂回 `applyEvent`：`applyEvent` 接受的是**原始条目**
   * （`depends_on` 而不是 `dependsOn`），用它重新投影已投影过的数据会在第二步
   * 丢掉依赖与认领者 —— 面板看起来"清单回来了，但依赖全没了"，而且不报错。
   */
  function setBoard(sessionId: string, board: TodoBoard, revision = 0): void {
    if (!sessionId || board.rows.length === 0) return
    boards.value = { ...boards.value, [sessionId]: { ...board, revision } }
  }

  /**
   * 采用一条 `todo_updated` 事件。
   *
   * `revision` 单调递增防护在这里落地：乱序到达的旧事件被丢弃，否则面板会
   * "倒退一格"然后永远差一格（多副本 SSE 与重连补发都会出现）。
   *
   * 返回是否真的采用了（供调用方决定要不要滚到可见处/提示）。
   */
  function applyEvent(
    sessionId: string,
    todos: unknown,
    revision?: unknown,
  ): boolean {
    if (!sessionId) return false
    const current = boards.value[sessionId]?.revision ?? 0
    if (!shouldApply({ revision }, current)) return false
    const next = projectTodoBoard(todos, Number.isFinite(Number(revision)) ? Number(revision) : current + 1)
    boards.value = { ...boards.value, [sessionId]: next }
    return true
  }

  /**
   * 清空某个会话的板子。
   *
   * 刻意提供显式入口而不是自动判断：新回合开始时是否清空是**产品决策**。
   * 现在选的是"新回合开始时不自动清空"——长任务跨多轮追问，清单应当延续；
   * 只有明确的 `/clear`、切换会话、或引擎发来空清单才清。
   */
  function clear(sessionId: string): void {
    if (!sessionId || !(sessionId in boards.value)) return
    const next = { ...boards.value }
    delete next[sessionId]
    boards.value = next
  }

  function clearAll(): void {
    boards.value = {}
  }

  /** 取某个会话的板子（无则空板，模板无需判空）。 */
  function boardOf(sessionId: string): TodoBoard {
    return boards.value[sessionId] ?? EMPTY_BOARD
  }

  /** 是否有可展示的清单（空板不渲染，避免占位）。 */
  function hasTodos(sessionId: string): boolean {
    return boardOf(sessionId).rows.length > 0
  }

  return {
    boards,
    collapsed: computed(() => collapsed.value),
    toggleCollapsed,
    applyEvent,
    setBoard,
    clear,
    clearAll,
    boardOf,
    hasTodos,
  }
}
