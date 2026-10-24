<script setup lang="ts">
import { Skeleton } from 'ant-design-vue'
import { computed } from 'vue'

interface Props {
  /** 骨架类型：list 列表行、cards 卡片网格、detail 详情头部+正文、table 表格 */
  variant?: 'list' | 'cards' | 'detail' | 'table'
  /** list/table 行数 */
  rows?: number
  /** cards 列数 */
  columns?: number
  /** 是否带标题栏骨架 */
  header?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  variant: 'list',
  rows: 5,
  columns: 3,
  header: true,
})

const cardCount = computed(() => props.columns * 2)
</script>

<template>
  <div
    class="page-skeleton"
    role="status"
    aria-live="polite"
    aria-busy="true"
  >
    <!-- 标题栏 -->
    <div
      v-if="header"
      class="sk-header"
    >
      <Skeleton
        :title="{ width: '32%' }"
        :loading="true"
        active
        :paragraph="false"
      />
      <Skeleton
        :title="{ width: '14%' }"
        :loading="true"
        active
        :paragraph="false"
        style="margin-top: 6px"
      />
    </div>

    <!-- 列表行 -->
    <template v-if="variant === 'list'">
      <div
        v-for="i in rows"
        :key="i"
        class="sk-row"
      >
        <Skeleton
          avatar
          :title="{ width: '40%' }"
          :loading="true"
          active
          :paragraph="{ width: ['24%'], rows: 1 }"
        />
      </div>
    </template>

    <!-- 卡片网格 -->
    <template v-else-if="variant === 'cards'">
      <div
        class="sk-grid"
        :style="{ '--cols': columns }"
      >
        <div
          v-for="i in cardCount"
          :key="i"
          class="sk-card"
        >
          <Skeleton
            :title="false"
            :loading="true"
            active
            :paragraph="{ rows: 3, width: ['100%', '80%', '52%'] }"
          />
        </div>
      </div>
    </template>

    <!-- 详情页 -->
    <template v-else-if="variant === 'detail'">
      <div class="sk-detail-head">
        <Skeleton
          avatar
          :avatar-size="56"
          :title="{ width: '30%' }"
          :loading="true"
          active
          :paragraph="{ rows: 1, width: ['18%'] }"
        />
      </div>
      <div class="sk-detail-body">
        <Skeleton
          v-for="i in rows"
          :key="i"
          :title="false"
          :loading="true"
          active
          :paragraph="{ rows: 1, width: ['100%', '92%', '76%'][i % 3] }"
        />
      </div>
    </template>

    <!-- 表格 -->
    <template v-else-if="variant === 'table'">
      <div class="sk-table">
        <div class="sk-table-head">
          <div
            v-for="i in columns + 1"
            :key="i"
            class="sk-cell"
          >
            <Skeleton
              :title="{ width: '60%' }"
              :loading="true"
              active
              :paragraph="false"
            />
          </div>
        </div>
        <div
          v-for="r in rows"
          :key="r"
          class="sk-table-row"
        >
          <div
            v-for="i in columns + 1"
            :key="i"
            class="sk-cell"
          >
            <Skeleton
              :title="{ width: `${40 + (i * 13) % 50}%` }"
              :loading="true"
              active
              :paragraph="false"
            />
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.page-skeleton { width: 100%; }

.sk-header { margin-bottom: 20px; }
.sk-header :deep(.ant-skeleton-title) { margin-bottom: 0; }

/* 列表行 */
.sk-row {
  display: flex;
  align-items: center;
  padding: 14px 12px;
  border-radius: var(--sig-radius-card);
}
.sk-row + .sk-row { margin-top: 2px; }
.sk-row:hover { background: var(--bg-secondary); }

/* 卡片网格 */
.sk-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr));
  gap: 16px;
  margin-top: 16px;
}
.sk-card {
  padding: 16px;
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--sig-radius-card);
  background: var(--bg-card);
}

/* 详情 */
.sk-detail-head { margin-bottom: 24px; }
.sk-detail-body > * + * { margin-top: 12px; }

/* 表格 */
.sk-table {
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--sig-radius-card);
  overflow: hidden;
  margin-top: 16px;
}
.sk-table-head {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: 1fr;
  gap: 8px;
  padding: 12px;
  background: var(--bg-secondary);
  border-bottom: var(--hairline-width) solid var(--border-card);
}
.sk-table-row {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: 1fr;
  gap: 8px;
  padding: 12px;
  border-bottom: var(--hairline-width) solid var(--border-card);
}
.sk-table-row:last-child { border-bottom: none; }
.sk-cell { min-width: 0; }

/* 响应式：窄屏表格骨架减少列数视觉密度 */
@media (max-width: 768px) {
  .sk-table-head, .sk-table-row { gap: 6px; padding: 10px; }
  .sk-header { margin-bottom: 14px; }
}
</style>
