<script setup lang="ts">
/**
 * 把对话内容沉淀为长期记忆（L2 `user_memory_entries`）。
 *
 * 与「存入知识库」的区别：知识库是供 RAG 检索的文档，记忆是供**提示词注入**的
 * 结构化条目（slot + key + value）—— 它会出现在记忆页，可被编辑、参与冲突裁决，
 * 也会在后续对话里自动带上。因此这里要求用户给出 key 与分类，而不是整篇存档。
 */
import { computed, ref, watch } from 'vue'
import { Input, Modal, Select, Slider, message } from 'ant-design-vue'
import { MEMORY_SLOTS, upsertMemory, type MemorySlot } from '../../api/memory'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const props = defineProps<{
  open: boolean
  /** 要沉淀的内容（默认填进 value） */
  content: string
  /** 默认 key（一般用会话标题） */
  defaultKey?: string
}>()
const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'saved', entryId: string): void
  (e: 'needs-confirmation', payload: { key: string; oldValue: string; newValue: string }): void
}>()

const slot = ref<MemorySlot>('fact')
const key = ref('')
const value = ref('')
const confidence = ref(80)
const saving = ref(false)

watch(
  () => props.open,
  (open) => {
    if (!open) return
    slot.value = 'fact'
    key.value = props.defaultKey?.trim() || ''
    value.value = props.content.trim()
    confidence.value = 80
  },
)

const canSave = computed(
  () => !!key.value.trim() && !!value.value.trim() && !saving.value,
)

async function save() {
  if (!canSave.value) return
  saving.value = true
  try {
    const res = await upsertMemory({
      slot: slot.value,
      key: key.value.trim(),
      value: value.value.trim(),
      confidence: confidence.value,
      // 用户亲手记下的内容即「用户确认」，后端据此把它排在派生值之前
      source: 'user_confirmed',
    })

    // 后端检测到与已确认值冲突时不会直接覆盖，而是登记待裁决 —— 如实告知，
    // 否则用户会以为「记住了」，实际记忆页里还挂着一条待办。
    if (res.conflict) {
      emit('needs-confirmation', {
        key: key.value.trim(),
        oldValue: String(res.conflict.old_value ?? ''),
        newValue: String(res.conflict.new_value ?? ''),
      })
      message.warning(t('errors.conflicts_with_confirmed_memory_logged_for_review_please_handle_on_the_memory_page'))
    } else if (res.duplicate_of) {
      message.warning(
        t('检测到相似记忆「{key}」，可在记忆页智能整理时合并', { key: res.duplicate_of.key }),
      )
    } else {
      message.success(t('memory.remembered_this_memory_will_be_attached_automatically_in_future_conversations'))
    }

    emit('saved', res.entry?.id ?? '')
    emit('update:open', false)
  } catch (e) {
    // axios 错误：后端给的 message 在 response.data.error，取不到就用兜底文案
    const detail = (e as { response?: { data?: { error?: unknown } } }).response?.data?.error
    message.error(typeof detail === 'string' && detail ? detail : t('errors.failed_to_save_memory'))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal
    :open="open"
    :title="$t('common.remember_this')"
    :confirm-loading="saving"
    :ok-button-props="{ disabled: !canSave }"
    :ok-text="$t('common.remember')"
    :cancel-text="$t('common.cancel')"
    @ok="save"
    @cancel="emit('update:open', false)"
  >
    <div class="save-memory">
      <p class="save-memory-hint">
        {{ $t('knowledge.notes_you_save_are_automatically_attached_in_future_conversations_distinct_from_save_to_knowledge_base_which_stores_documents_for_retrieval') }}
      </p>

      <div class="save-memory-field">
        <label class="save-memory-label">{{ $t('common.category') }}</label>
        <Select
          v-model:value="slot"
          style="width: 100%"
        >
          <Select.Option
            v-for="s in MEMORY_SLOTS"
            :key="s.slot"
            :value="s.slot"
          >
            {{ s.label }}
          </Select.Option>
        </Select>
      </div>

      <div class="save-memory-field">
        <label class="save-memory-label">{{ $t('common.key_2') }}</label>
        <Input
          v-model:value="key"
          :placeholder="$t('memory.what_is_this_memory_about_e_g_stack_tech_preference')"
        />
      </div>

      <div class="save-memory-field">
        <label class="save-memory-label">{{ $t('common.content_value') }}</label>
        <Input.TextArea
          v-model:value="value"
          :rows="5"
          :placeholder="$t('common.content_to_remember_long_term')"
        />
      </div>

      <div class="save-memory-field">
        <label class="save-memory-label">{{ $t('置信度 {n}', { n: confidence }) }}</label>
        <Slider
          v-model:value="confidence"
          :min="0"
          :max="100"
        />
      </div>
    </div>
  </Modal>
</template>

<style scoped>
.save-memory {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.save-memory-hint {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--text-secondary);
}
.save-memory-label {
  display: block;
  margin-bottom: var(--space-1);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-secondary);
}
</style>
