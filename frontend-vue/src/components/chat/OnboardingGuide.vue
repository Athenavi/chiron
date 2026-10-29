<script setup lang="ts">
/**
 * 新用户引导（P0-3）的浮层。
 *
 * ## 为什么是 Modal 而不是 antd `Tour`
 *
 * `Tour` 需要被测元素此刻就挂在 DOM 上并可见；三步里的"选模型""发消息"分别属于
 * 不同区域，且空会话时侧栏可能还没渲染 —— 用 `Tour` 会导出一个位置乱跳的引导。
 * `Tour` 已用于**被拦下时**的带路（`ChatSetupTour.vue`），职责不同，不合并。
 *
 * ## 决策不在这里
 *
 * "该不该弹、哪一步待办"，全在 `composables/useNewUserOnboarding.ts` ⇒ 那部分可单测。
 * 本组件只做展示与把点击转成事件，进度落库由 ChatView 持有的 composable 负责。
 */
import { computed } from 'vue'
import { Modal } from 'ant-design-vue'
import { t } from '../../i18n'
import type { OnboardingStatus, OnboardingStep } from '../../api/onboarding'
import { currentStepOf, stepStatusOf } from '../../composables/useNewUserOnboarding'

const props = defineProps<{ open: boolean; status: OnboardingStatus | null }>()
const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'skip-all'): void
  (e: 'focus', target: 'model' | 'input'): void
}>()

const current = computed(() => currentStepOf(props.status))

interface Row {
  step: OnboardingStep
  title: string
  desc: string
  status: 'pending' | 'completed' | 'skipped'
  action?: { label: string; target: 'model' | 'input' }
}

const rows = computed<Row[]>(() => {
  const dailyLimit = props.status?.trial?.daily_free_limit ?? 0
  const credits = props.status?.trial?.credits
  const trialDesc = credits === undefined
    ? t('chat.onboarding_step_trial_desc', { n: dailyLimit })
    : `${t('chat.onboarding_step_trial_desc', { n: dailyLimit })} ${t('chat.onboarding_credits', { credits })}`
  return [
    {
      step: 'trial_credit',
      title: t('chat.onboarding_step_trial_title'),
      desc: trialDesc,
      status: stepStatusOf(props.status, 'trial_credit'),
    },
    {
      step: 'default_model',
      title: t('chat.onboarding_step_model_title'),
      desc: t('chat.onboarding_step_model_desc'),
      status: stepStatusOf(props.status, 'default_model'),
      action: { label: t('chat.onboarding_go_pick_model'), target: 'model' },
    },
    {
      step: 'first_message',
      title: t('chat.onboarding_step_message_title'),
      desc: t('chat.onboarding_step_message_desc'),
      status: stepStatusOf(props.status, 'first_message'),
      action: { label: t('chat.onboarding_go_send'), target: 'input' },
    },
  ]
})

function close(): void {
  emit('update:open', false)
}

/** 点某一步的动作：先收起浮层再聚焦，否则浮层会盖住要指的位置 */
function onAction(target: 'model' | 'input'): void {
  close()
  emit('focus', target)
}
</script>

<template>
  <Modal
    :open="open"
    :title="t('chat.onboarding_title')"
    :footer="null"
    :width="520"
    @cancel="close"
  >
    <p class="onboarding-desc">
      {{ t('chat.onboarding_desc') }}
    </p>
    <ol class="onboarding-steps">
      <li
        v-for="row in rows"
        :key="row.step"
        class="onboarding-step"
        :class="{ 'is-current': row.step === current, 'is-done': row.status !== 'pending' }"
      >
        <div class="step-body">
          <div class="step-title">
            {{ row.title }}
            <span
              v-if="row.status === 'completed'"
              class="step-tag is-completed"
            >{{ t('chat.onboarding_done') }}</span>
            <span
              v-else-if="row.status === 'skipped'"
              class="step-tag is-skipped"
            >{{ t('chat.onboarding_skipped') }}</span>
          </div>
          <div class="step-desc">
            {{ row.desc }}
          </div>
        </div>
        <button
          v-if="row.action && row.status === 'pending'"
          type="button"
          class="step-action"
          @click="onAction(row.action.target)"
        >
          {{ row.action.label }}
        </button>
      </li>
    </ol>
    <div class="onboarding-footer">
      <button
        type="button"
        class="onboarding-skip"
        @click="emit('skip-all')"
      >
        {{ t('chat.onboarding_skip_all') }}
      </button>
      <button
        type="button"
        class="onboarding-ok"
        @click="close"
      >
        {{ t('chat.onboarding_got_it') }}
      </button>
    </div>
  </Modal>
</template>

<style scoped>
.onboarding-desc {
  margin: 0 0 16px;
  color: var(--text-secondary, #6b7280);
  line-height: 1.6;
}

.onboarding-steps {
  margin: 0;
  padding: 0;
  list-style: none;
}

.onboarding-step {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  justify-content: space-between;
  padding: 12px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 8px;
  margin-bottom: 8px;
}

/* 当前待办那一步给一点强调：用户一眼知道"卡在哪" */
.onboarding-step.is-current {
  border-color: var(--color-primary, #1677ff);
}

/* 已完成/已跳过降低视觉权重，但保留在列表里 —— 用户能看到自己走到哪 */
.onboarding-step.is-done .step-title {
  color: var(--text-secondary, #6b7280);
}

.step-title {
  font-weight: 600;
  margin-bottom: 4px;
}

.step-desc {
  color: var(--text-secondary, #6b7280);
  font-size: 13px;
  line-height: 1.5;
}

.step-tag {
  margin-inline-start: 8px;
  font-size: 12px;
  font-weight: 400;
}

.step-tag.is-completed {
  color: var(--color-success, #16a34a);
}

.step-tag.is-skipped {
  color: var(--text-secondary, #6b7280);
}

.step-action {
  flex: none;
  align-self: center;
  padding: 4px 12px;
  border: 1px solid var(--color-primary, #1677ff);
  border-radius: 6px;
  background: transparent;
  color: var(--color-primary, #1677ff);
  cursor: pointer;
}

.onboarding-footer {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  margin-top: 16px;
}

.onboarding-skip,
.onboarding-ok {
  padding: 6px 16px;
  border-radius: 6px;
  cursor: pointer;
}

.onboarding-skip {
  border: 1px solid var(--border-color, #e5e7eb);
  background: transparent;
}

.onboarding-ok {
  border: 1px solid var(--color-primary, #1677ff);
  background: var(--color-primary, #1677ff);
  color: #fff;
}
</style>
