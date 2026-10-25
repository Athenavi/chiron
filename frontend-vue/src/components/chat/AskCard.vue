<script setup lang="ts">
import { ref, computed, watch } from 'vue'

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

const emit = defineEmits<{
  (e: 'answer', value: string): void
  (e: 'decline'): void
  (e: 'drafting', active: boolean): void
}>()

const draft = ref('')

// 已经开始作答就不该被超时判定（与上游 #12884 同款语义）：把草稿状态上报给父层，
// 由它决定"这项不再算超时"。空草稿 = 未开始，照常超时。
watch(draft, () => emit('drafting', draft.value.trim().length > 0))

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

/** 明确拒绝：不是"没回答"，而是一条**答复** —— 模型据此改问法或自行决定。 */
function decline() {
  if (props.expired) return
  emit('decline')
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
        class="ask-option btn-lg"
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
        class="ask-submit btn-lg"
        type="button"
        :disabled="expired || !draft.trim()"
        @click="submitDraft"
      >
        {{ $t('common.answer') }}
      </button>
      <button
        class="ask-decline"
        type="button"
        :disabled="expired"
        @click="decline"
      >
        {{ $t('common.decline') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.ask-card {
  padding: 10px 14px;
  border: var(--hairline-width) solid var(--border); border-inline-start: 3px solid var(--accent);
  border-radius: var(--sig-radius-card);
  background: var(--bg-card);
}
.ask-card.expired { opacity: 0.66; }
.ask-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.ask-tag { font-size: var(--fs-xs); color: var(--accent); background: var(--accent-bg); padding: 2px 8px; border-radius: var(--sig-radius-card); }
.ask-expired { font-size: var(--fs-xs); color: var(--text-tertiary); }
.ask-question { margin: 0 0 10px; font-size: 13px; line-height: 1.6; color: var(--text-primary); }
.ask-options { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.ask-option {
  /* 尺寸走 style.css §4 的 .btn-lg 阶梯；这里只留配色与发丝环 */
  background: var(--bg-surface); color: var(--text-primary);
  box-shadow: inset 0 0 0 var(--hairline-width) var(--border);
  transition: box-shadow var(--dur-fast) ease, color var(--dur-fast) ease, background var(--dur-fast) ease;
}
.ask-option:hover:not(:disabled) { box-shadow: inset 0 0 0 var(--hairline-width) var(--accent); color: var(--accent); }
.ask-option:disabled { opacity: 0.5; cursor: not-allowed; }
/* 按下塌陷（CDS 胶囊形变族），见 style.css §5 */
.ask-option:active:not(:disabled) { animation: cdsPillDip var(--dur-snap) var(--ease-snap); }
/* 入场：复用 .pill-pop 的两段式过冲 —— 选项是被"递"到用户面前的，弹一下比直接出现
   更贴合它"等你选"的语义。:active 时由上面的 dip 接管（同名属性后者赢）。 */
.ask-option { animation: cdsPillPopUp calc(var(--dur-normal) * .4) var(--ease-out), cdsPillPopDown calc(var(--dur-normal) * .6) var(--ease-overshoot) calc(var(--dur-normal) * .4); }
.ask-option:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ask-free { display: flex; gap: 8px; }
.ask-input {
  flex: 1; min-width: 0;
  border: var(--hairline-width) solid var(--border); border-radius: var(--sig-radius-button);
  background: var(--bg-input); color: var(--text-primary);
  font-size: 13px; line-height: 20px; padding: 5px 10px;
}
.ask-input:focus { outline: none; border-color: var(--accent); }
.ask-input:disabled { opacity: 0.5; }
.ask-submit {
  flex: none;
  /* 尺寸走 .btn-lg 阶梯；配色留在本组件 */
  border: none;
  background: var(--accent); color: var(--on-solid);
}
.ask-submit:disabled { opacity: 0.4; cursor: not-allowed; }
.ask-submit:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: var(--focus-ring-offset); }
@media (max-width: 576px) { .ask-options { gap: 6px; } .ask-option { padding: 5px 10px; } }
</style>
