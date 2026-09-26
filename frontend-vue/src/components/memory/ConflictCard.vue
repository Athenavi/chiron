<template>
  <div
    v-if="conflict"
    class="conflict-card"
  >
    <div class="conflict-header">
      <span class="conflict-icon">⚠️</span>
      <span class="conflict-title">{{ $t('记忆冲突：{slot} · {key}', { slot: formatSlot(conflict.slot), key: conflict.item_key }) }}</span>
      <span class="conflict-time">{{ formatTime(conflict.created_at) }}</span>
    </div>

    <div class="conflict-body">
      <div class="conflict-values">
        <div class="value-item old">
          <div class="value-label">
            {{ $t('common.current_value') }}
          </div>
          <div class="value-content">
            {{ conflict.old_value }}
          </div>
        </div>
        <div class="value-arrow">
          →
        </div>
        <div class="value-item new">
          <div class="value-label">
            {{ $t('admin.rediscover') }}
          </div>
          <div class="value-content">
            {{ conflict.new_value }}
          </div>
        </div>
      </div>
      <div class="conflict-desc">
        {{ $t('errors.ai_found_content_conflicting_with_your_confirmed_info_your_ruling_is_needed') }}
      </div>
    </div>

    <div class="conflict-actions">
      <button
        class="btn-keep"
        :disabled="resolving"
        @click="resolve('keep_old')"
      >
        {{ $t('common.keep_current_value') }}
      </button>
      <button
        class="btn-use"
        :disabled="resolving"
        @click="resolve('use_new')"
      >
        {{ $t('common.use_new_value') }}
      </button>
      <button
        class="btn-manual"
        :disabled="resolving"
        @click="showManual = true"
      >
        {{ $t('common.manual_edit') }}
      </button>
      <button
        class="btn-dismiss"
        :disabled="resolving"
        @click="dismiss"
      >
        {{ $t('common.ignore') }}
      </button>
    </div>

    <!-- 手动修改对话框 -->
    <div
      v-if="showManual"
      class="manual-dialog"
    >
      <input
        v-model="manualValue"
        type="text"
        :placeholder="$t('common.enter_new_value')"
        class="manual-input"
      >
      <div class="manual-actions">
        <button
          :disabled="resolving"
          @click="showManual = false"
        >
          {{ $t('common.cancel') }}
        </button>
        <button
          :disabled="resolving"
          @click="resolve('manual', manualValue)"
        >
          {{ $t('common.confirm_2') }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { resolveConflict, deleteConflict, type MemoryConflict } from '@/api/memory'

const { t } = useI18n()

const props = defineProps<{
  conflict: MemoryConflict
}>()

const emit = defineEmits<{
  resolved: [conflictId: string]
  dismissed: [conflictId: string]
}>()

const resolving = ref(false)
const showManual = ref(false)
const manualValue = ref('')

function formatSlot(slot: string): string {
  const map: Record<string, string> = {
    identity: t('admin.identity'),
    preference: t('settings.preferences'),
    decision: t('common.key_decisions'),
    fact: t('common.fact'),
  }
  return map[slot] || slot
}

function formatTime(timestamp: number): string {
  const date = new Date(timestamp * 1000)
  const now = new Date()
  const diff = now.getTime() - date.getTime()
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return t('common.just_now')
  if (minutes < 60) return t('{n} 分钟前', { n: minutes })
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return t('{n} 小时前', { n: hours })
  const days = Math.floor(hours / 24)
  return t('{n} 天前', { n: days })
}

async function resolve(resolution: 'keep_old' | 'use_new' | 'manual', manualValue?: string) {
  if (resolving.value) return
  resolving.value = true
  try {
    await resolveConflict(props.conflict.conflict_id, resolution, manualValue)
    emit('resolved', props.conflict.conflict_id)
  } catch {
    // 静默失败，UI 保持当前状态
  } finally {
    resolving.value = false
    showManual.value = false
  }
}

async function dismiss() {
  if (resolving.value) return
  resolving.value = true
  try {
    await deleteConflict(props.conflict.conflict_id)
    emit('dismissed', props.conflict.conflict_id)
  } catch {
    // 静默失败，UI 保持当前状态
  } finally {
    resolving.value = false
  }
}
</script>

<style scoped>
.conflict-card {
  background: var(--warning-bg);
  border: 1px solid var(--warning);
  border-radius: var(--radius-lg);
  padding: 16px;
  margin: 12px 0;
}

.conflict-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.conflict-icon {
  font-size: 18px;
}

.conflict-title {
  font-weight: 600;
  color: var(--warning);
  flex: 1;
}

.conflict-time {
  font-size: 12px;
  color: var(--text-tertiary);
}

.conflict-body {
  margin-bottom: 12px;
}

.conflict-values {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.value-item {
  flex: 1;
  padding: 8px;
  border-radius: var(--radius-sm);
}

.value-item.old {
  background: var(--error-bg);
  border: 1px solid var(--error);
}

.value-item.new {
  background: var(--info-bg);
  border: 1px solid var(--info);
}

.value-label {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}

.value-content {
  font-weight: 500;
  word-break: break-all;
}

.value-arrow {
  font-size: 20px;
  color: var(--text-secondary);
}

.conflict-desc {
  font-size: 13px;
  color: var(--text-secondary);
}

.conflict-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.conflict-actions button {
  padding: 8px 16px;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 14px;
  transition: opacity var(--dur-normal);
}

.conflict-actions button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-keep {
  background: var(--error);
  color: var(--on-solid);
}

.btn-use {
  background: var(--info);
  color: var(--on-solid);
}

.btn-manual {
  background: var(--success);
  color: var(--on-solid);
}

.btn-dismiss {
  background: var(--text-tertiary);
  color: var(--on-solid);
}

.manual-dialog {
  margin-top: 12px;
  padding: 12px;
  background: var(--bg-surface);
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-default);
}

.manual-input {
  width: 100%;
  padding: 8px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  margin-bottom: 8px;
}

.manual-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

.manual-actions button {
  padding: 6px 12px;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  border-radius: var(--radius-sm);
  cursor: pointer;
}
</style>