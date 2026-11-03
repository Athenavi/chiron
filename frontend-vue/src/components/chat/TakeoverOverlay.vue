<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Button, Modal } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import type { Takeover } from '../../api/superagent'

const props = defineProps<{ takeover: Takeover }>()
const emit = defineEmits<{ (e: 'stop', id: string): void }>()

const { t } = useI18n()

/** 倒计时靠本地 tick 刷新，不依赖快照轮询 —— 秒级变化没必要打服务端。 */
const now = ref(Date.now())
let timer: number | undefined

onMounted(() => {
  timer = window.setInterval(() => { now.value = Date.now() }, 1000)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})

const stopping = computed(() => props.takeover.status === 'stopping')

const remaining = computed(() => {
  const ends = new Date(props.takeover.ends_at).getTime()
  if (Number.isNaN(ends)) return '—'
  const left = Math.max(0, ends - now.value)
  const h = Math.floor(left / 3600000)
  const m = Math.floor((left % 3600000) / 60000)
  const s = Math.floor((left % 60000) / 1000)
  return h > 0 ? `${h}h ${m}m` : `${m}m ${s}s`
})

const budgetLeft = computed(() => {
  const cap = props.takeover.token_cap
  if (!cap || cap <= 0) return t('agent.superagent_overlay_unlimited')
  return Math.max(0, cap - props.takeover.token_spent).toLocaleString()
})

/** 已运行时长：接管从 started_at 起算，与剩余时长共用同一个 tick。 */
const elapsed = computed(() => {
  const start = new Date(props.takeover.started_at).getTime()
  if (Number.isNaN(start)) return '—'
  const dur = Math.max(0, now.value - start)
  const h = Math.floor(dur / 3600000)
  const m = Math.floor((dur % 3600000) / 60000)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
})

function confirmStop() {
  // 二次确认：打断会取消正在跑的回合，一次误点就丢一整拍的工作。
  Modal.confirm({
    title: t('agent.superagent_stop_title'),
    content: t('agent.superagent_stop_text'),
    okText: t('agent.superagent_stop'),
    okType: 'danger',
    cancelText: t('common.cancel'),
    onOk: () => emit('stop', props.takeover.id),
  })
}
</script>

<template>
  <div class="takeover-overlay">
    <div class="takeover-card">
      <div class="takeover-status">
        <span class="takeover-dot" />
        {{ stopping ? $t('agent.superagent_overlay_stopping') : $t('agent.superagent_overlay_title') }}
      </div>

      <div class="takeover-stats">
        <div class="takeover-stat">
          <span class="stat-label">{{ $t('agent.superagent_overlay_elapsed') }}</span>
          <span class="stat-value">{{ elapsed }}</span>
        </div>
        <div class="takeover-stat">
          <span class="stat-label">{{ $t('agent.superagent_overlay_remaining') }}</span>
          <span class="stat-value">{{ remaining }}</span>
        </div>
        <div class="takeover-stat">
          <span class="stat-label">{{ $t('agent.superagent_overlay_spent') }}</span>
          <span class="stat-value">{{ takeover.token_spent.toLocaleString() }}</span>
        </div>
        <div class="takeover-stat">
          <span class="stat-label">{{ $t('agent.superagent_overlay_budget') }}</span>
          <span class="stat-value">{{ budgetLeft }}</span>
        </div>
      </div>

      <p class="takeover-hint">
        {{ $t('agent.superagent_overlay_input_locked') }}
      </p>

      <Button
        danger
        :disabled="stopping"
        @click="confirmStop"
      >
        {{ $t('agent.superagent_stop') }}
      </Button>
    </div>
  </div>
</template>

<style scoped>
.takeover-overlay {
  position: absolute;
  inset: 0;
  z-index: var(--z-overlay);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-overlay);
  backdrop-filter: blur(2px);
}
.takeover-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 28px 32px;
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  text-align: center;
  max-width: 420px;
}
.takeover-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
}
.takeover-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--primary);
  animation: takeover-pulse var(--dur-pulse) ease-in-out infinite;
}
@keyframes takeover-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}
.takeover-stats {
  display: flex;
  gap: 24px;
}
.takeover-stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.stat-label {
  font-size: 12px;
  color: var(--text-tertiary);
}
.stat-value {
  font-size: 15px;
  font-weight: 500;
}
.takeover-hint {
  margin: 0;
  font-size: 12px;
  color: var(--text-tertiary);
  line-height: 1.5;
}
</style>
