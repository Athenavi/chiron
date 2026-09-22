<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { CaretRightOutlined } from '@ant-design/icons-vue'

const props = defineProps<{ content: string; streaming?: boolean }>()

/** 离底容差：与主列表锚定取同一个值（覆盖亚像素滚动与底部 padding） */
const BOTTOM_ANCHOR_EPSILON_PX = 48

const expanded = ref(false)
/** 用户是否手动切换过折叠（一旦手动干预，自动策略不再覆盖他的选择） */
const userToggled = ref(false)
const bodyRef = ref<HTMLElement | null>(null)
/** 用户滚动权：true = 跟随最新思考；用户上滚即解除（与主列表 `following` 同一语义） */
const following = ref(true)

// 折叠摘要：running 跟随最新行，完成显示首行（deepseek ReasoningRow 语义）
const summary = computed(() => {
  const text = props.content.trimEnd()
  if (props.streaming) {
    const nl = text.lastIndexOf('\n')
    return nl === -1 ? text : text.slice(nl + 1)
  }
  const nl = text.indexOf('\n')
  return nl === -1 ? text : text.slice(0, nl)
})

function toggle() {
  userToggled.value = true
  expanded.value = !expanded.value
}

/**
 * 自动折叠策略（对应 ZCode `reasoning.tsx` 的 `shouldAutoCollapseReasoning`）：
 *
 * - **流式开始 → 展开**：让用户看得见思考过程（原先默认折叠，等于把过程藏起来）；
 * - **流式结束 → 自动折叠**：不占版面；
 * - 但**用户手动切换过就不再自动改变** —— 与主列表滚动的 `following` 同一哲学：
 *   **自动行为只在用户没表达过意图时生效**。
 */
watch(
  () => props.streaming,
  (now) => {
    if (userToggled.value) return
    expanded.value = !!now
  },
  { immediate: true },
)

/**
 * 思考区**自己的**滚动锚定。
 *
 * 与主列表（`transcriptAnchor`）和 ZCode 的 `timelineScrollAnchor` 是同一套模型：
 * 只有**真实用户滚动**能改变 `following`；程序化贴底不改（否则会自我打架）。
 */
function onBodyScroll() {
  const el = bodyRef.value
  if (!el) return
  const distance = el.scrollHeight - el.clientHeight - el.scrollTop
  following.value = distance <= BOTTOM_ANCHOR_EPSILON_PX
}

/** 内容增长：跟随中 → 贴底；已解除 → **保持阅读位置（绝不拉回）** */
watch(
  () => props.content,
  async () => {
    if (!following.value || !expanded.value) return
    await nextTick()
    const el = bodyRef.value
    if (el) el.scrollTop = el.scrollHeight
  },
)

/** 恢复跟随（"回到底部"按钮，与主列表同名语义） */
async function stickToBottom() {
  following.value = true
  await nextTick()
  const el = bodyRef.value
  if (el) el.scrollTop = el.scrollHeight
}
</script>

<template>
  <div
    class="reasoning-row chat-row-shell"
    :data-state="streaming ? 'running' : 'ok'"
  >
    <button
      class="reasoning-main chat-row-head"
      type="button"
      :aria-expanded="expanded"
      @click="toggle"
    >
      <CaretRightOutlined
        class="chat-chevron"
        :class="{ open: expanded }"
      />
      <span
        class="think-icon"
        aria-hidden
      >💭</span>
      <span class="think-label">Think</span>
      <span
        class="chat-sep"
        aria-hidden
      />
      <span
        class="chat-state-dot"
        :data-state="streaming ? 'running' : 'done'"
        aria-hidden
      />
      <span class="think-summary">{{ summary }}</span>
    </button>
    <Transition name="chat-expand">
      <div
        v-if="expanded"
        class="think-body-wrap"
      >
        <!-- 思考体自己滚动：长思考不再把整页撑长 -->
        <div
          ref="bodyRef"
          class="think-body"
          @scroll.passive="onBodyScroll"
        >
          {{ content }}
        </div>
        <!-- 用户上滚离底时给出口，避免他"找不到最新思考"而困惑 -->
        <button
          v-if="!following"
          type="button"
          class="think-bottom"
          @click="stickToBottom"
        >
          {{ $t('回到底部') }}
        </button>
      </div>
    </Transition>
  </div>
</template>

<!-- 行节奏 / chevron / 状态点 / 展开动画 / reduced-motion 均由全局 .chat-* 原语提供
     （src/style.css），此处只保留 Think 行独有的内容样式 -->
<style scoped>
.think-icon { font-size: 12px; flex-shrink: 0; }
.think-label { font-weight: 600; color: var(--text-primary); font-size: 13px; }
.think-summary { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--text-tertiary); font-size: 12px; text-align: left; }
.reasoning-row[data-state='running'] .think-summary { color: var(--primary); }
.think-body-wrap { position: relative; }
.think-body {
  padding: 10px 12px; margin-top: 2px;
  /* 自身滚动：长思考只在这一块内滚，不把整页撑长 */
  max-height: 40vh; overflow: auto;
  font-size: 13px; line-height: 1.7; color: var(--text-secondary);
  background: var(--bg-secondary); border: 1px solid var(--border-card);
  border-radius: var(--sig-radius-button); white-space: pre-wrap;
}
.think-bottom {
  position: absolute; right: 10px; bottom: 10px;
  padding: 3px 8px;
  border: 1px solid var(--border-card); border-radius: var(--radius-full);
  background: var(--bg-page); color: var(--text-secondary);
  font-size: 11px; cursor: pointer;
}
.think-bottom:hover { color: var(--text-primary); }
</style>
