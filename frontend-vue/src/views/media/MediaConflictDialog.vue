<script setup lang="ts">
/**
 * 粘贴冲突对话框（Windows 式：替换 / 跳过 / 保留两者 + 全部应用）。
 *
 * 文案与预览名全部来自 `dialogs.buildConflictDialog()`（纯函数，有单测）——
 * 尤其是 `keep-both` 的预览名：用户必须在**确认之前**看到它会变成 `a (2).txt`，
 * 否则只能赌自己选完会得到什么。
 *
 * 为什么默认是"跳过"而不是"替换"：默认替换意味着用户只要点得快一点，
 * 目标处已有文件就被覆盖了，而且**不可撤销**。宁可少做一个动作。
 */
import { computed, ref, watch } from 'vue'
import { Checkbox, Modal, Select } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'

import { uniqueName, type ConflictChoice } from './clipboard'
import { buildConflictDialog } from './dialogs'

const props = defineProps<{
  open: boolean
  conflicts: readonly { id: string; name: string }[]
  /** 目标目录已有的名字（用于算 keep-both 预览名） */
  existingNames: ReadonlySet<string>
  /** 正在执行粘贴 */
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  /** 逐项处置结果；调用方据此生成粘贴计划 */
  (e: 'confirm', choices: Map<string, ConflictChoice>): void
}>()

const { t } = useI18n()

/** 每个冲突项的当前选择。 */
const choices = ref<Map<string, ConflictChoice>>(new Map())
/** "对全部冲突应用此选择"：当它为真时，改一行会同步到所有行。 */
const applyAll = ref(false)

// 每次打开都重置：上次的处置不该延续到下一次粘贴 ——
// 否则用户会以为自己在逐项确认，实际是被上一次的"全部替换"直接套用了。
watch(
  () => props.open,
  (open) => {
    if (!open) return
    const fresh = new Map<string, ConflictChoice>()
    for (const c of props.conflicts) fresh.set(c.id, 'skip')
    choices.value = fresh
    applyAll.value = false
  },
  { immediate: true },
)

const dialog = computed(() =>
  buildConflictDialog(props.conflicts, choices.value, props.existingNames, uniqueName),
)

const choiceOptions = computed(() => [
  { value: 'replace', label: t('media.conflict_replace') },
  { value: 'skip', label: t('media.conflict_skip') },
  { value: 'keep-both', label: t('media.conflict_keep_both') },
])

function setChoice(id: string, value: ConflictChoice): void {
  const next = new Map(choices.value)
  if (applyAll.value) {
    // 全部应用：一次改到所有行。这是 Windows 的关键设计 ——
    // 一次粘贴 50 个冲突项时，不提供它就要点 50 次。
    for (const key of next.keys()) next.set(key, value)
  } else {
    next.set(id, value)
  }
  choices.value = next
}

function close(): void {
  if (props.loading) return
  emit('update:open', false)
}

function confirm(): void {
  if (props.loading) return
  emit('confirm', new Map(choices.value))
}
</script>

<template>
  <Modal
    :open="props.open"
    :title="t(dialog.titleKey)"
    :ok-text="t('common.confirm')"
    :cancel-text="t('common.cancel')"
    :confirm-loading="props.loading"
    :mask-closable="!props.loading"
    :width="560"
    @ok="confirm"
    @cancel="close"
  >
    <p class="conflict-message">
      {{ t(dialog.messageKey, dialog.params) }}
    </p>

    <ul class="conflict-list">
      <li
        v-for="row in dialog.rows"
        :key="row.id"
        class="conflict-row"
      >
        <span
          class="conflict-name"
          :title="row.name"
        >{{ row.name }}</span>
        <!--
          keep-both 时显示将要用的名字：用户必须在确认前看到结果，
          否则只能赌自己选完会变成什么。
        -->
        <span
          v-if="row.choice === 'keep-both'"
          class="conflict-preview"
        >→ {{ row.previewName }}</span>
        <Select
          :value="row.choice"
          :options="choiceOptions"
          size="small"
          class="conflict-select"
          @change="(v: unknown) => setChoice(row.id, v as ConflictChoice)"
        />
      </li>
    </ul>

    <Checkbox
      v-if="dialog.showApplyAll"
      v-model:checked="applyAll"
      class="conflict-apply-all"
    >
      {{ t('media.conflict_apply_all') }}
    </Checkbox>
  </Modal>
</template>

<style scoped>
.conflict-message {
  margin: 0 0 12px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.conflict-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 40vh;
  overflow-y: auto;
}

.conflict-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-block: 6px;
  border-block-end: var(--hairline-width) solid var(--border-subtle);
}

.conflict-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary);
}

.conflict-preview {
  flex: none;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.conflict-select {
  flex: none;
  width: 120px;
}

.conflict-apply-all {
  margin-block-start: 12px;
}
</style>
