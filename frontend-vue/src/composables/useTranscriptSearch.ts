/**
 * 会话内检索（正文与思考；工具输出不进结果），从 `ChatView.vue` 抽出。
 *
 * 边界：只管检索框的开关 / 查询串 / 命中游标，既不持有消息列表，也不负责高亮怎么画 ——
 * 命中跳转交给注入的 `focus`，由调用方接到既有的 focusToken 链路上
 * （含高亮闪烁与滚动归因）。
 *
 * 依赖用 getter / 回调而非值：本块在 ChatView 里声明得比 `items`、`trajectoryFocus`
 * 更早，靠"调用那一刻才求值"避开 TDZ（"used before its declaration"）。拆分后必须
 * 保留这个性质 —— 一旦把 `items` 当值传进来，就会在模块求值阶段读到未初始化的绑定。
 */
import { computed, nextTick, ref, watch } from 'vue'

import { findMatches } from '../components/chat/transcriptSearch'
import type { ChatItem } from '../components/chat/chat-types'

export interface TranscriptSearchDeps {
  /** 当前会话的消息列表（getter：调用时才求值） */
  items: () => readonly ChatItem[]
  /** 跳到第 n 条命中（调用方负责高亮与滚动） */
  focus: (index: number) => void
}

export function useTranscriptSearch(deps: TranscriptSearchDeps) {
  const { items, focus } = deps

  const searchOpen = ref(false)
  const searchQuery = ref('')
  const searchCursor = ref(0)
  const searchInputRef = ref<HTMLInputElement | null>(null)
  const searchMatches = computed(() => findMatches(items(), searchQuery.value))
  watch(searchQuery, () => {
    searchCursor.value = 0
  })

  function openSearch() {
    searchOpen.value = true
    nextTick(() => searchInputRef.value?.focus())
  }

  function closeSearch() {
    searchOpen.value = false
    searchQuery.value = ''
  }

  /** 循环跳转：命中项用既有的 focusToken 链路（含高亮闪烁与滚动归因） */
  function gotoMatch(delta: number) {
    const hits = searchMatches.value
    if (!hits.length) return
    searchCursor.value = (searchCursor.value + delta + hits.length) % hits.length
    focus(hits[searchCursor.value]!)
  }

  return {
    searchOpen,
    searchQuery,
    searchCursor,
    searchInputRef,
    searchMatches,
    openSearch,
    closeSearch,
    gotoMatch,
  }
}
