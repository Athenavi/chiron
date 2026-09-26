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
    message.error(e?.response?.data?.error || t('errors.query_failed'))
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
  { title: t('common.time'), dataIndex: 'created_at', key: 'created_at', width: 180, customRender: ({ text }) => formatTime(text) },
  { title: t('admin.user_2'), dataIndex: 'user_id', key: 'user_id', width: 140, ellipsis: true },
  { title: t('common.action_2'), dataIndex: 'action', key: 'action', width: 140 },
  { title: t('common.resource_type'), dataIndex: 'resource_type', key: 'resource_type', width: 140 },
  { title: t('common.resource_id'), dataIndex: 'resource_id', key: 'resource_id', width: 180, ellipsis: true },
  { title: 'IP', dataIndex: 'ip_address', key: 'ip_address', width: 140 },
  { title: t('common.action'), key: 'action_btn', width: 80, fixed: 'right' },
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
        {{ $t('admin.operation_audit') }}
      </h2>
      <p class="audit-desc">
        {{ $t('admin.query_range_is_limited_to_the_last_7_days_to_ensure_index_performance') }}
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
        :placeholder="$t('admin.user_id')"
        allow-clear
        class="u-full-sm"
        style="width: 180px"
        @press-enter="onSearch"
      />
      <a-input
        v-model:value="filters.action"
        :placeholder="$t('admin.action_e_g_post_v1_ent_privacy')"
        allow-clear
        class="u-full-sm"
        style="width: 280px"
        @press-enter="onSearch"
      />
      <a-input
        v-model:value="filters.resource_type"
        :placeholder="$t('common.resource_type')"
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
        {{ $t('common.query') }}
      </a-button>
      <a-button
        class="u-full-sm"
        @click="onReset"
      >
        {{ $t('auth.reset') }}
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
            {{ $t('common.details') }}
          </a-button>
        </template>
      </template>
    </a-table>

    <a-drawer
      v-model:open="detailVisible"
      :title="$t('admin.audit_log_details')"
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
          <a-descriptions-item :label="$t('common.time')">
            {{ formatTime(currentLog.created_at) }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('admin.tenant')">
            {{ currentLog.tenant_id }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('admin.user_2')">
            {{ currentLog.user_id || '-' }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('common.action_2')">
            {{ currentLog.action }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('common.resource_type')">
            {{ currentLog.resource_type }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('common.resource_id')">
            {{ currentLog.resource_id || '-' }}
          </a-descriptions-item>
          <a-descriptions-item label="IP">
            {{ currentLog.ip_address || '-' }}
          </a-descriptions-item>
        </a-descriptions>
        <div class="audit-detail-block">
          <div class="audit-detail-label">
            {{ $t('common.details_details') }}
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
