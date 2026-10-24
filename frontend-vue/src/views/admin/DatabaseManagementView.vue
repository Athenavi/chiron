<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Card, Table, Button, Space, Tag, Input, Alert,
  Statistic, Descriptions, Spin, message,
} from 'ant-design-vue'
import {
  SearchOutlined, ThunderboltOutlined,
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
interface DatabaseStatus {
  version?: string
  connected?: boolean
}
/** SQL 查询结果（POST /v1/admin/database/query → { columns, rows, count, truncated }） */
interface QueryResult {
  columns?: unknown[]
  rows?: unknown[]
  count?: number
  truncated?: boolean
}
/** 列描述可能是字符串，也可能是 `{ name }` 对象（后端两条链路形状不同） */
function columnName(c: unknown, fallback: string): string {
  const name = (c ?? {}) as { name?: unknown }
  return typeof name.name === 'string' && name.name ? name.name : fallback
}

const statusData = ref<DatabaseStatus | null>(null)
const statusLoading = ref(false)

async function loadStatus() {
  statusLoading.value = true
  try {
    const resp = await api.get('/v1/admin/database/status')
    statusData.value = resp.data?.data || {}
  } catch (e) {
    message.error(apiErrorMessage(e, t('errors.failed_to_fetch_database_status')))
  } finally {
    statusLoading.value = false
  }
}

// ── 配置 ──
// GET /v1/admin/database/configs → { configs: { k: v } }
const configs = ref<Record<string, unknown>>({})
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
  } catch (e) {
    message.error(apiErrorMessage(e, t('errors.failed_to_fetch_database_config')))
  } finally {
    configLoading.value = false
  }
}

// ── SQL 查询器（只读）──
// POST /v1/admin/database/query { query } → { columns, rows, count, truncated }
const queryText = ref('')
const querying = ref(false)
const queryResult = ref<QueryResult | null>(null)

const queryColumns = computed(() => {
  const cols = queryResult.value?.columns || []
  return cols.map((c, i) => ({
    title: typeof c === 'string' ? c : columnName(c, t('common.column_n', { n: i + 1 })),
    dataIndex: typeof c === 'string' ? c : columnName(c, `col_${i}`),
    ellipsis: true,
  }))
})

const queryRows = computed(() => {
  const cols = queryResult.value?.columns || []
  const rows = queryResult.value?.rows || []
  return rows.map((r, i) => {
    if (Array.isArray(r)) {
      const obj: Record<string, unknown> = {}
      cols.forEach((c, j) => {
        obj[columnName(c, `col_${j}`)] = r[j]
      })
      obj.__row = i
      return obj
    }
    return { ...(r as Record<string, unknown>), __row: i }
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
  } catch (e) {
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
    message.success(t('common.optimization_complete_action_table_status', { action: d.action || action, table: d.table || table, status: d.status || 'ok' }))
  } catch (e) {
    message.error(apiErrorMessage(e, t('errors.optimization_failed')))
  } finally {
    optimizing.value = false
  }
}

// ── 工具函数 ──
function cellText(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

onMounted(async () => {
  await Promise.allSettled([loadStatus(), loadConfigs()])
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

      <!--
        备份：已移除容器内备份/恢复（pg_dump/psql）。
        企业部署使用云数据库，备份由服务商承担（快照 / PITR）—— 这里只保留说明，
        不再提供创建/恢复入口（后端对应端点也已删除）。
      -->
      <Card
        :title="$t('common.backup_list')"
        style="margin-bottom: 16px"
      >
        <Alert
          type="info"
          show-icon
          :message="$t('common.backups_are_managed_by_your_database_provider')"
        />
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
            {{ $t('common.clear_2') }}
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
.page-header h1 { margin: 0; font-size: 24px; font-weight: var(--fw-semibold); color: var(--text-primary); }
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
