<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import dayjs, { type Dayjs } from 'dayjs'
import { queryAuditLogs } from '../../api/audit'
import type { AuditLog } from '../../api/audit'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const loading = ref(false)
const logs = ref<AuditLog[]>([])
const total = ref(0)
const detailVisible = ref(false)
const currentLog = ref<AuditLog | null>(null)

// 过滤条件（时间默认最近 7 天，对齐后端强制范围）
// 注意：a-range-picker 的值须为 dayjs 对象，避免内部比较触发 date.isAfter 报错
const filters = reactive({
  user_id: '',
  action: '',
  resource_type: '',
  dateRange: undefined as [Dayjs, Dayjs] | undefined,
})

const pagination = reactive({
  page: 1,
  page_size: 50,
})

function defaultRange(): [Dayjs, Dayjs] {
  const to = dayjs()
  const from = to.subtract(6, 'day')
  return [from, to]
}

function fmt(d: Dayjs): string {
  return d.format('YYYY-MM-DD')
}

async function fetchLogs() {
  loading.value = true
  try {
    const range = filters.dateRange?.length === 2 ? filters.dateRange : defaultRange()
    const res = await queryAuditLogs({
      user_id: filters.user_id || undefined,
      action: filters.action || undefined,
      resource_type: filters.resource_type || undefined,
      from: fmt(range[0]),
      to: fmt(range[1]),
      page: pagination.page,
      page_size: pagination.page_size,
    })
    logs.value = res.data
    total.value = res.total
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('查询失败'))
  } finally {
    loading.value = false
  }
}

function onSearch() {
  pagination.page = 1
  fetchLogs()
}

function onReset() {
  filters.user_id = ''
  filters.action = ''
  filters.resource_type = ''
  filters.dateRange = defaultRange()
  pagination.page = 1
  fetchLogs()
}

function onPageChange(page: number, pageSize: number) {
  pagination.page = page
  pagination.page_size = pageSize
  fetchLogs()
}

function showDetail(log: AuditLog) {
  currentLog.value = log
  detailVisible.value = true
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

function formatDetails(d: unknown): string {
  if (d === null || d === undefined) return ''
  if (typeof d === 'string') return d
  try { return JSON.stringify(d, null, 2) } catch { return String(d) }
}

const columns: TableColumnsType = [
  { title: t('时间'), dataIndex: 'created_at', key: 'created_at', width: 180, customRender: ({ text }) => formatTime(text) },
  { title: t('用户'), dataIndex: 'user_id', key: 'user_id', width: 140, ellipsis: true },
  { title: t('动作'), dataIndex: 'action', key: 'action', width: 140 },
  { title: t('资源类型'), dataIndex: 'resource_type', key: 'resource_type', width: 140 },
  { title: t('资源 ID'), dataIndex: 'resource_id', key: 'resource_id', width: 180, ellipsis: true },
  { title: 'IP', dataIndex: 'ip_address', key: 'ip_address', width: 140 },
  { title: t('操作'), key: 'action_btn', width: 80, fixed: 'right' },
]

onMounted(() => {
  filters.dateRange = defaultRange()
  fetchLogs()
})
</script>

<template>
  <div class="audit-view">
    <div class="audit-header">
      <h2 class="audit-title">
        {{ $t('操作审计') }}
      </h2>
      <p class="audit-desc">
        {{ $t('查询范围限制为 7 天内，确保命中索引性能。') }}
      </p>
    </div>

    <div class="audit-filters">
      <a-range-picker
        v-model:value="filters.dateRange"
        :format="'YYYY-MM-DD'"
        class="u-full-sm"
        style="width: 260px"
      />
      <a-input
        v-model:value="filters.user_id"
        :placeholder="$t('用户 ID')"
        allow-clear
        class="u-full-sm"
        style="width: 180px"
        @press-enter="onSearch"
      />
      <a-input
        v-model:value="filters.action"
        :placeholder="$t('动作（如 POST /v1/ent/privacy）')"
        allow-clear
        class="u-full-sm"
        style="width: 280px"
        @press-enter="onSearch"
      />
      <a-input
        v-model:value="filters.resource_type"
        :placeholder="$t('资源类型')"
        allow-clear
        class="u-full-sm"
        style="width: 160px"
        @press-enter="onSearch"
      />
      <a-button
        type="primary"
        class="u-full-sm"
        @click="onSearch"
      >
        {{ $t('查询') }}
      </a-button>
      <a-button
        class="u-full-sm"
        @click="onReset"
      >
        {{ $t('重置') }}
      </a-button>
    </div>

    <a-table
      :columns="columns"
      :data-source="logs"
      :loading="loading"
      :row-key="(r: AuditLog) => r.id"
      :pagination="{
        current: pagination.page,
        pageSize: pagination.page_size,
        total,
        showSizeChanger: true,
        pageSizeOptions: ['20', '50', '100'],
        showTotal: (n: number) => t('共 {n} 条', { n }),
      }"
      :scroll="{ x: 1100 }"
      size="small"
      @change="(p: any) => onPageChange(p.current, p.pageSize)"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'action_btn'">
          <a-button
            type="link"
            size="small"
            @click="showDetail(record as AuditLog)"
          >
            {{ $t('详情') }}
          </a-button>
        </template>
      </template>
    </a-table>

    <a-drawer
      v-model:open="detailVisible"
      :title="$t('审计日志详情')"
      width="560"
      placement="right"
    >
      <template v-if="currentLog">
        <a-descriptions
          :column="1"
          size="small"
          bordered
        >
          <a-descriptions-item label="ID">
            {{ currentLog.id }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('时间')">
            {{ formatTime(currentLog.created_at) }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('租户')">
            {{ currentLog.tenant_id }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('用户')">
            {{ currentLog.user_id || '-' }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('动作')">
            {{ currentLog.action }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('资源类型')">
            {{ currentLog.resource_type }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('资源 ID')">
            {{ currentLog.resource_id || '-' }}
          </a-descriptions-item>
          <a-descriptions-item label="IP">
            {{ currentLog.ip_address || '-' }}
          </a-descriptions-item>
        </a-descriptions>
        <div class="audit-detail-block">
          <div class="audit-detail-label">
            {{ $t('详情（details）') }}
          </div>
          <pre class="audit-detail-json">{{ formatDetails(currentLog.details) || '-' }}</pre>
        </div>
      </template>
    </a-drawer>
  </div>
</template>

<style scoped>
.audit-view {
  padding: 16px 24px;
}
.audit-header {
  margin-bottom: 16px;
}
.audit-title {
  margin: 0;
  font-size: 20px;
}
.audit-desc {
  margin: 4px 0 0;
  color: var(--text-tertiary);
  font-size: 13px;
}
.audit-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}
.audit-detail-block {
  margin-top: 16px;
}
.audit-detail-label {
  margin-bottom: 8px;
  font-weight: 500;
}
.audit-detail-json {
  margin: 0;
  padding: 12px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-md);
  font-size: 12px;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

/* 窄屏：筛选表单竖排全宽，输入/按钮提高触控高度 */
@media (max-width: 576px) {
  .audit-view { padding: 12px; }
  .audit-filters :deep(.ant-input),
  .audit-filters :deep(.ant-picker),
  .audit-filters :deep(.ant-btn) {
    height: 40px;
  }
}
</style>
