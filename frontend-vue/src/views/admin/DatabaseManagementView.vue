<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Card, Table, Button, Space, Tag, Input, Popconfirm, Alert,
  Statistic, Descriptions, Spin, message,
} from 'ant-design-vue'
import {
  ReloadOutlined, PlusOutlined, SearchOutlined, ThunderboltOutlined,
  DatabaseOutlined,
} from '@ant-design/icons-vue'
import { api } from '../../api'
import { apiErrorMessage } from '../../composables/useCrudResource'
import EmptyState from '../../components/common/EmptyState.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const TextArea = Input.TextArea

const initialLoading = ref(true)

// ── 数据库状态 ──
// GET /v1/admin/database/status → { version, connected }
const statusData = ref<any>(null)
const statusLoading = ref(false)

async function loadStatus() {
  statusLoading.value = true
  try {
    const resp = await api.get('/v1/admin/database/status')
    statusData.value = resp.data?.data || {}
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.failed_to_fetch_database_status')))
  } finally {
    statusLoading.value = false
  }
}

// ── 配置 ──
// GET /v1/admin/database/configs → { configs: { k: v } }
const configs = ref<Record<string, any>>({})
const configLoading = ref(false)

const configRows = computed(() =>
  Object.entries(configs.value || {}).map(([key, value]) => ({
    key,
    value: value && typeof value === 'object' ? JSON.stringify(value) : String(value ?? ''),
  }))
)

const configColumns = [
  { title: t('common.config_item'), dataIndex: 'key', key: 'key', ellipsis: true },
  { title: t('common.value'), dataIndex: 'value', key: 'value', ellipsis: true },
]

async function loadConfigs() {
  configLoading.value = true
  try {
    const resp = await api.get('/v1/admin/database/configs')
    configs.value = resp.data?.data?.configs || {}
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.failed_to_fetch_database_config')))
  } finally {
    configLoading.value = false
  }
}

// ── 备份 ──
// GET /v1/admin/database/backups → { backups: [{ name, size, time }] }
// POST /v1/admin/database/backups → { name, status }
// POST /v1/admin/database/backups/{name}/restore
const backups = ref<any[]>([])
const backupsLoading = ref(false)
const creatingBackup = ref(false)
const restoringName = ref<string | null>(null)

const backupColumns = [
  { title: t('common.name'), dataIndex: 'name', key: 'name', ellipsis: true },
  { title: t('common.size'), key: 'size', width: 120 },
  { title: t('common.time'), key: 'time', width: 190 },
  { title: t('common.action'), key: 'actions', width: 110, fixed: 'right' as const },
]

async function loadBackups() {
  backupsLoading.value = true
  try {
    const resp = await api.get('/v1/admin/database/backups')
    backups.value = resp.data?.data?.backups || []
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.failed_to_fetch_backup_list')))
  } finally {
    backupsLoading.value = false
  }
}

async function createBackup() {
  creatingBackup.value = true
  try {
    const resp = await api.post('/v1/admin/database/backups')
    const d = resp.data?.data || {}
    message.success(t('备份已创建（{name}，状态：{status}）', { name: d.name || '—', status: d.status || 'pending' }))
    await loadBackups()
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.failed_to_create_backup')))
  } finally {
    creatingBackup.value = false
  }
}

async function restoreBackup(record: any) {
  restoringName.value = record.name
  try {
    await api.post(`/v1/admin/database/backups/${encodeURIComponent(record.name)}/restore`)
    message.success(t('正在从备份「{name}」恢复，请稍后刷新查看结果', { name: record.name }))
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.restore_failed')))
  } finally {
    restoringName.value = null
  }
}

// ── SQL 查询器（只读）──
// POST /v1/admin/database/query { query } → { columns, rows, count, truncated }
const queryText = ref('')
const querying = ref(false)
const queryResult = ref<any>(null)

const queryColumns = computed(() => {
  const cols = queryResult.value?.columns || []
  return cols.map((c: any, i: number) => ({
    title: typeof c === 'string' ? c : (c?.name || t('列 {n}', { n: i + 1 })),
    dataIndex: typeof c === 'string' ? c : (c?.name || `col_${i}`),
    ellipsis: true,
  }))
})

const queryRows = computed(() => {
  const cols = queryResult.value?.columns || []
  const rows = queryResult.value?.rows || []
  return rows.map((r: any, i: number) => {
    if (Array.isArray(r)) {
      const obj: Record<string, any> = {}
      cols.forEach((c: any, j: number) => {
        const k = typeof c === 'string' ? c : (c?.name || `col_${j}`)
        obj[k] = r[j]
      })
      obj.__row = i
      return obj
    }
    return { ...r, __row: i }
  })
})

async function executeQuery() {
  const sql = queryText.value.trim()
  if (!sql) {
    message.warning(t('common.please_enter_a_sql_query'))
    return
  }
  querying.value = true
  try {
    const resp = await api.post('/v1/admin/database/query', { query: sql })
    queryResult.value = resp.data?.data || null
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.query_execution_failed')))
  } finally {
    querying.value = false
  }
}

// ── 优化 ──
// POST /v1/admin/database/optimize/{action: analyze|vacuum} { table }
const optimizeTable = ref('')
const optimizing = ref(false)

async function runOptimize(action: 'analyze' | 'vacuum') {
  const table = optimizeTable.value.trim()
  if (!table) {
    message.warning(t('common.please_enter_the_table_name_to_optimize'))
    return
  }
  optimizing.value = true
  try {
    const resp = await api.post(`/v1/admin/database/optimize/${action}`, { table })
    const d = resp.data?.data || {}
    message.success(t('优化完成：{action} {table}（{status}）', { action: d.action || action, table: d.table || table, status: d.status || 'ok' }))
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.optimization_failed')))
  } finally {
    optimizing.value = false
  }
}

// ── 工具函数 ──
function formatSize(s: any): string {
  if (s == null || s === '') return '-'
  if (typeof s === 'number') {
    if (s >= 1024 * 1024 * 1024) return `${(s / (1024 * 1024 * 1024)).toFixed(2)} GB`
    if (s >= 1024 * 1024) return `${(s / (1024 * 1024)).toFixed(2)} MB`
    if (s >= 1024) return `${(s / 1024).toFixed(2)} KB`
    return `${s} B`
  }
  return String(s)
}

function formatTime(t: any): string {
  return t ? new Date(t).toLocaleString('zh-CN') : '-'
}

function cellText(v: any): string {
  if (v == null) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

onMounted(async () => {
  await Promise.allSettled([loadStatus(), loadConfigs(), loadBackups()])
  initialLoading.value = false
})
</script>

<template>
  <div class="database-management">
    <div class="page-header">
      <h1>{{ $t('admin.database_management') }}</h1>
      <Space>
        <Button @click="loadStatus">
          {{ $t('common.refresh_status') }}
        </Button>
        <Button @click="loadBackups">
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('common.refresh_backup') }}
        </Button>
      </Space>
    </div>

    <Spin :spinning="initialLoading">
      <!-- 状态卡 -->
      <Card
        :title="$t('admin.database_status')"
        style="margin-bottom: 16px"
        :loading="statusLoading"
      >
        <Descriptions
          :column="2"
          bordered
          size="small"
        >
          <Descriptions.Item :label="$t('common.version')">
            <Space>
              <DatabaseOutlined />
              {{ statusData?.version || '-' }}
            </Space>
          </Descriptions.Item>
          <Descriptions.Item :label="$t('common.connection_status')">
            <Tag :color="statusData?.connected ? 'green' : 'red'">
              {{ statusData?.connected ? $t('common.connected') : $t('common.not_connected') }}
            </Tag>
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <!-- 配置表 -->
      <Card
        :title="$t('admin.database_config')"
        style="margin-bottom: 16px"
      >
        <Table
          :columns="configColumns"
          :data-source="configRows"
          :loading="configLoading"
          row-key="key"
          :pagination="false"
          :scroll="{ x: 600 }"
          size="small"
        >
          <template #emptyText>
            <EmptyState :description="$t('common.no_config_items_yet')" />
          </template>
        </Table>
      </Card>

      <!-- 备份列表 -->
      <Card style="margin-bottom: 16px">
        <template #title>
          <Space>{{ $t('common.backup_list') }}</Space>
        </template>
        <template #extra>
          <Button
            type="primary"
            :loading="creatingBackup"
            @click="createBackup"
          >
            <template #icon>
              <PlusOutlined />
            </template>
            {{ $t('common.create_backup') }}
          </Button>
        </template>
        <Table
          :columns="backupColumns"
          :data-source="backups"
          :loading="backupsLoading"
          row-key="name"
          :pagination="{ pageSize: 10, showSizeChanger: true }"
          :scroll="{ x: 640 }"
        >
          <template #emptyText>
            <EmptyState
              :description="$t('common.no_backups_yet')"
              :hint="$t('common.click_create_backup_at_the_top_right_to_make_a_backup')"
            />
          </template>

          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'size'">
              {{ formatSize(record.size) }}
            </template>
            <template v-else-if="column.key === 'time'">
              {{ formatTime(record.time) }}
            </template>
            <template v-else-if="column.key === 'actions'">
              <Popconfirm
                :title="$t('admin.warning_restoring_from_backup_will_overwrite_the_current_database_this_cannot_be_undone_continue')"
                :ok-text="$t('common.confirm_restore')"
                :cancel-text="$t('common.cancel')"
                @confirm="restoreBackup(record)"
              >
                <Button
                  size="small"
                  danger
                  :loading="restoringName === record.name"
                >
                  {{ $t('common.restore') }}
                </Button>
              </Popconfirm>
            </template>
          </template>
        </Table>
      </Card>

      <!-- SQL 查询器 -->
      <Card
        :title="$t('common.sql_query_read_only')"
        style="margin-bottom: 16px"
      >
        <Alert
          type="info"
          show-icon
          :message="$t('common.read_only_queries_only_select_etc_no_write_operations_will_be_executed')"
          style="margin-bottom: 16px"
        />
        <TextArea
          v-model:value="queryText"
          :rows="4"
          placeholder="SELECT * FROM ... LIMIT 100;"
          style="font-family: monospace; margin-bottom: 12px"
        />
        <Space style="margin-bottom: 16px">
          <Button
            type="primary"
            :loading="querying"
            @click="executeQuery"
          >
            <template #icon>
              <SearchOutlined />
            </template>
            {{ $t('common.run_query') }}
          </Button>
          <Button @click="queryResult = null; queryText = ''">
            {{ $t('common.empty') }}
          </Button>
        </Space>

        <template v-if="queryResult">
          <Space
            style="margin-bottom: 8px"
            wrap
          >
            <Statistic
              :title="$t('common.row_count')"
              :value="queryResult.count ?? (queryResult.rows || []).length"
            />
            <Tag
              v-if="queryResult.truncated"
              color="orange"
            >
              {{ $t('common.result_truncated') }}
            </Tag>
          </Space>
          <Table
            :columns="queryColumns"
            :data-source="queryRows"
            :row-key="(_r: any, i?: number) => i ?? 0"
            :pagination="{ pageSize: 20, showSizeChanger: true }"
            :scroll="{ x: 800 }"
            size="small"
          >
            <template #emptyText>
              <EmptyState :description="$t('common.query_returned_no_results')" />
            </template>
            <template #bodyCell="{ column, record }">
              <span class="cell">{{ cellText(record[(column as any).dataIndex as string]) }}</span>
            </template>
          </Table>
        </template>
        <template v-else>
          <EmptyState :description="$t('common.query_results_will_appear_here')" />
        </template>
      </Card>

      <!-- 优化区 -->
      <Card
        :title="$t('admin.performance_optimization')"
        style="margin-bottom: 16px"
      >
        <Alert
          type="warning"
          show-icon
          :message="$t('admin.optimization_consumes_database_resources_run_it_on_specific_tables_during_off_peak_hours')"
          style="margin-bottom: 16px"
        />
        <Space wrap>
          <Input
            v-model:value="optimizeTable"
            :placeholder="$t('admin.enter_table_name_e_g_users')"
            style="width: 240px"
            @press-enter="runOptimize('analyze')"
          />
          <Button
            :loading="optimizing"
            @click="runOptimize('analyze')"
          >
            <template #icon>
              <ThunderboltOutlined />
            </template>
            {{ $t('common.analyze_updates_statistics') }}
          </Button>
          <Button
            :loading="optimizing"
            @click="runOptimize('vacuum')"
          >
            <template #icon>
              <ThunderboltOutlined />
            </template>
            {{ $t('common.vacuum_reclaims_storage_space') }}
          </Button>
        </Space>
      </Card>
    </Spin>
  </div>
</template>

<style scoped>
.database-management { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
.page-header h1 { margin: 0; font-size: 24px; font-weight: 600; color: var(--text-primary); }
.cell { word-break: break-all; }

@media (max-width: 768px) {
  .database-management { padding: 16px 12px; }
  .database-management .page-header { row-gap: 12px; }
  .database-management .page-header .ant-btn { min-height: 40px; }
}
@media (max-width: 480px) {
  .database-management .page-header h1 { font-size: 20px; }
}
</style>
