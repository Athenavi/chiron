<script setup lang="ts">
import { ref, computed } from 'vue'

export interface AskOption {
  label: string
  value?: string
}

/**
 * 结构化提问卡：模型通过 ask_user 工具向用户要一个明确的回答。
 *
 * 与审批卡的区别：审批只需要「允许/拒绝」二值，而提问需要**携带答案回来**
 * （选项值或自由文本），所以走独立的 answer 通道而不是复用 approval。
 */
const props = withDefaults(defineProps<{
  question: string
  options?: (string | AskOption)[]
  /** 有选项时是否仍允许自由输入（默认允许） */
  allowFreeText?: boolean
  /** 后端等待超时后置位：保留问题上下文但禁用交互 */
  expired?: boolean
}>(), {
  options: () => [],
  allowFreeText: true,
  expired: false,
})

const emit = defineEmits<{ (e: 'answer', value: string): void }>()

const draft = ref('')

const normalized = computed<AskOption[]>(() =>
  (props.options ?? []).map(option => (typeof option === 'string' ? { label: option, value: option } : option)),
)
const hasOptions = computed(() => normalized.value.length > 0)
// 注意：Boolean prop 未传时 Vue 会强转为 false，所以默认值必须由 withDefaults 给
const showFreeText = computed(() => !hasOptions.value || props.allowFreeText)

function choose(option: AskOption) {
  if (props.expired) return
  emit('answer', option.value ?? option.label)
}

function submitDraft() {
  const text = draft.value.trim()
  if (!text || props.expired) return
  emit('answer', text)
}
</script>

<template>
  <div
    class="ask-card"
    :class="{ expired }"
  >
    <div class="ask-head">
      <span class="ask-tag">{{ $t('common.needs_your_answer') }}</span>
      <span
        v-if="expired"
        class="ask-expired"
      >{{ $t('errors.timed_out_you_can_continue_the_conversation') }}</span>
    </div>
    <p class="ask-question">
      {{ question }}
    </p>
    <div
      v-if="hasOptions"
      class="ask-options"
    >
      <button
        v-for="option in normalized"
        :key="option.label"
        class="ask-option"
        type="button"
        :disabled="expired"
        @click="choose(option)"
      >
        {{ option.label }}
      </button>
    </div>
    <div
      v-if="showFreeText"
      class="ask-free"
    >
      <input
        v-model="draft"
        class="ask-input"
        type="text"
        :placeholder="hasOptions ? $t('common.you_can_also_type_your_answer_directly') : $t('common.type_your_answer_and_press_enter')"
        :disabled="expired"
        :aria-label="$t('common.answer')"
        @keydown.enter.prevent="submitDraft"
      >
      <button
        class="ask-submit"
        type="button"
        :disabled="expired || !draft.trim()"
        @click="submitDraft"
      >
        {{ $t('common.answer') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.ask-card {
  padding: 10px 14px;
  border: 1px solid var(--border); border-left: 3px solid var(--accent);
  border-radius: 10px;
  background: var(--bg-card);
}
.ask-card.expired { opacity: 0.66; }
.ask-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.ask-tag { font-size: 11px; color: var(--accent); background: var(--accent-bg); padding: 2px 8px; border-radius: 10px; }
.ask-expired { font-size: 11px; color: var(--text-tertiary); }
.ask-question { margin: 0 0 10px; font-size: 13px; line-height: 1.6; color: var(--text-primary); }
.ask-options { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.ask-option {
  border: 1px solid var(--border); border-radius: var(--sig-radius-button);
  background: var(--bg-surface); color: var(--text-primary);
  font-size: 13px; line-height: 20px; padding: 5px 14px; cursor: pointer;
  transition: border-color var(--dur-fast) ease, color var(--dur-fast) ease, background var(--dur-fast) ease;
}
.ask-option:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.ask-option:disabled { opacity: 0.5; cursor: not-allowed; }
.ask-option:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ask-free { display: flex; gap: 8px; }
.ask-input {
  flex: 1; min-width: 0;
  border: 1px solid var(--border); border-radius: var(--sig-radius-button);
  background: var(--bg-input); color: var(--text-primary);
  font-size: 13px; line-height: 20px; padding: 5px 10px;
}
.ask-input:focus { outline: none; border-color: var(--accent); }
.ask-input:disabled { opacity: 0.5; }
.ask-submit {
  flex: none;
  border: none; border-radius: var(--sig-radius-button);
  background: var(--accent); color: var(--on-solid);
  font-size: 13px; line-height: 20px; padding: 5px 16px; cursor: pointer;
}
.ask-submit:disabled { opacity: 0.4; cursor: not-allowed; }
.ask-submit:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
@media (max-width: 576px) { .ask-options { gap: 6px; } .ask-option { padding: 5px 10px; } }
</style>
