<script setup lang="ts">
/**
 * 属性面板（Windows「属性」对话框的对应物）。
 *
 * 字段由 `properties.buildPropertyRows()` 生成（纯函数，有单测）—— 这里只负责
 * 渲染与"复制到剪贴板"这一个副作用。
 *
 * 两个刻意的设计：
 *
 * 1. **长值单行省略 + 悬停看全 + 一键复制**。路径与 URL 是这条面板最常用的内容，
 *    而它们必然很长；换行显示会把面板撑成一大坨，反而更难读。
 * 2. **底部动作与右键菜单共用同一套动作名**（`PROPERTY_ACTIONS`），可用性仍由
 *    `menuModel` 求值 —— 否则"菜单里灰着、属性面板里能点"这类漂移迟早出现。
 */
import { computed } from 'vue'
import { Button, Tooltip, message, type TooltipProps } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'

import { formatDateTime } from '../../utils/datetime'
import { buildPropertyRows, PROPERTY_ACTIONS, type PropertySource } from './properties'
import { entryOf, type MenuAction, type MenuModel } from './menuModel'
import { LABEL_KEYS } from './menuModel'

const props = defineProps<{
  /** 要展示的资产 */
  source: PropertySource
  /** 菜单模型（与右键菜单同一份，保证可用性判断一致） */
  model: MenuModel
}>()

const emit = defineEmits<{
  (e: 'action', action: MenuAction): void
}>()

const { t } = useI18n()

/** 时间统一走 `utils/datetime`，与项目其他页面同格式（不渲染原始 ISO 串）。 */
const rows = computed(() =>
  buildPropertyRows(props.source, (value) => formatDateTime(value, '—')),
)

/**
 * 底部动作的可执行性。
 *
 * 直接复用 `entryOf` + `disabledReason`：与右键菜单逐字同一判断，
 * 避免两处条件各写一遍后漂移。
 */
const actions = computed(() =>
  PROPERTY_ACTIONS.map((action) => {
    const entry = entryOf(props.model, action)
    return {
      action,
      label: t(LABEL_KEYS[action]),
      disabled: !entry || entry.disabledReason !== '',
      reason: entry?.disabledReason ? t(entry.disabledReason) : '',
    }
  }),
)

/**
 * 复制到剪贴板。
 *
 * 失败要提示而不是静默：`navigator.clipboard` 在非安全上下文（http 局域网访问）
 * 下**根本不存在**，静默失败会让用户以为复制成功了，粘贴出旧内容。
 */
async function copyValue(value: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value)
    message.success(t('media.property_copied'))
  } catch {
    message.warning(t('media.property_copy_failed'))
  }
}

/** 禁用原因作为 tooltip（与菜单一致：只灰不解释等于让用户猜）。 */
function tooltipOf(reason: string): TooltipProps['title'] {
  return reason || undefined
}
</script>

<template>
  <div class="props">
    <dl class="props-list">
      <div
        v-for="row in rows"
        :key="row.key"
        class="props-row"
      >
        <dt class="props-label">
          {{ t(row.labelKey) }}
        </dt>
        <dd class="props-value">
          <span
            class="props-text"
            :class="{ 'is-truncated': row.truncate }"
            :title="row.truncate ? row.value : undefined"
          >{{ row.value }}</span>
          <Tooltip
            v-if="row.copyable"
            :title="t('media.property_copy')"
            placement="top"
          >
            <Button
              type="text"
              size="small"
              class="props-copy"
              :aria-label="t('media.property_copy')"
              @click="copyValue(row.value)"
            >
              ⧉
            </Button>
          </Tooltip>
        </dd>
      </div>
    </dl>

    <div class="props-actions">
      <Tooltip
        v-for="item in actions"
        :key="item.action"
        :title="tooltipOf(item.reason)"
        placement="top"
      >
        <Button
          size="small"
          :disabled="item.disabled"
          @click="emit('action', item.action)"
        >
          {{ item.label }}
        </Button>
      </Tooltip>
    </div>
  </div>
</template>

<style scoped>
.props {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.props-list {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.props-row {
  display: grid;
  grid-template-columns: minmax(80px, auto) 1fr;
  gap: 12px;
  align-items: baseline;
}

.props-label {
  margin: 0;
  color: var(--text-tertiary);
  font-size: var(--fs-xs);
  white-space: nowrap;
}

.props-value {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

.props-text {
  flex: 1;
  min-width: 0;
  color: var(--text-primary);
  word-break: break-word;
}

/* 长值单行省略：换行会把面板撑成一大坨，反而更难读 */
.props-text.is-truncated {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.props-copy {
  flex: none;
  color: var(--text-tertiary);
}

.props-copy:hover {
  color: var(--primary);
}

.props-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-block-start: 12px;
  border-block-start: var(--hairline-width) solid var(--border-subtle);
}
</style>
