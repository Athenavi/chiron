<script setup lang="ts">
/**
 * 删除确认对话框。
 *
 * 文案由 `dialogs.buildDeleteConfirm()` 生成（纯函数，有单测）—— 这里只负责渲染
 * 与"用户是否真的确认"。
 *
 * 两个刻意的设计：
 *
 * 1. **强确认用危险色的确定按钮**。颜色不是装饰：它让"这是破坏性操作"在视觉上
 *    先于文字被感知到。
 * 2. **递归项数 >= 50 时要求勾选"我已知晓"**。这不是为了增加摩擦，而是因为
 *    "删掉 47 个子项"这种事，用户点错一次的代价远高于多点一个勾。
 */
import { computed, ref, watch } from 'vue'
import { Checkbox, Modal } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'

import { buildDeleteConfirm, type DeleteScopeDetail } from './dialogs'

const props = defineProps<{
  open: boolean
  scope: DeleteScopeDetail
  /** 正在执行删除（按钮进入 loading，防止重复提交） */
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'confirm'): void
}>()

const { t } = useI18n()

/** 用户是否勾选了"我已知晓"（只在 requireAcknowledge 时生效）。 */
const acknowledged = ref(false)
const text = computed(() => buildDeleteConfirm(props.scope))

// 每次打开都重置：上一次的勾选不该延续到下一次删除 ——
// 否则第二次（可能是更危险的一次）会**跳过**那一步确认。
watch(
  () => props.open,
  (open) => {
    if (open) acknowledged.value = false
  },
)

const canConfirm = computed(
  () => !props.loading && (!text.value.requireAcknowledge || acknowledged.value),
)

const okText = computed(() => {
  const n = props.scope.selectedCount
  return n > 1 ? t('media.delete_confirm_ok_many', { n }) : t('media.delete_confirm_ok')
})

function close(): void {
  if (props.loading) return
  emit('update:open', false)
}

function confirm(): void {
  if (!canConfirm.value) return
  emit('confirm')
}
</script>

<template>
  <Modal
    :open="props.open"
    :title="t(text.titleKey)"
    :ok-text="okText"
    :cancel-text="t('common.cancel')"
    :ok-button-props="{ danger: text.danger === 'strong', disabled: !canConfirm }"
    :confirm-loading="props.loading"
    :mask-closable="!props.loading"
    @ok="confirm"
    @cancel="close"
  >
    <p class="delete-message">
      {{ t(text.messageKey, text.params) }}
    </p>

    <!--
      只有递归项数很大时才要求勾选。文案里已经写了精确数字，
      这里再要一次主动确认，是为了让顺手点"确定"的路径慢下来。
    -->
    <Checkbox
      v-if="text.requireAcknowledge"
      v-model:checked="acknowledged"
      class="delete-ack"
    >
      {{ t('media.delete_confirm_ack') }}
    </Checkbox>
  </Modal>
</template>

<style scoped>
.delete-message {
  margin: 0;
  color: var(--text-secondary);
  line-height: 1.6;
}

.delete-ack {
  margin-block-start: 12px;
  color: var(--text-primary);
  line-height: 1.5;
}
</style>
