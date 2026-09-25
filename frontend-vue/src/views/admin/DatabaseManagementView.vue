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
    message.error(apiErrorMessage(e, t('获取数据库状态失败')))
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
  { title: t('配置项'), dataIndex: 'key', key: 'key', ellipsis: true },
  { title: t('值'), dataIndex: 'value', key: 'value', ellipsis: true },
]

async function loadConfigs() {
  configLoading.value = true
  try {
    const resp = await api.get('/v1/admin/database/configs')
    configs.value = resp.data?.data?.configs || {}
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('获取数据库配置失败')))
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
  { title: t('名称'), dataIndex: 'name', key: 'name', ellipsis: true },
  { title: t('大小'), key: 'size', width: 120 },
  { title: t('时间'), key: 'time', width: 190 },
  { title: t('操作'), key: 'actions', width: 110, fixed: 'right' as const },
]

async function loadBackups() {
  backupsLoading.value = true
  try {
    const resp = await api.get('/v1/admin/database/backups')
    backups.value = resp.data?.data?.backups || []
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('获取备份列表失败')))
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
    message.error(apiErrorMessage(e, t('创建备份失败')))
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
    message.error(apiErrorMessage(e, t('恢复失败')))
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
    message.warning(t('请输入 SQL 查询语句'))
    return
  }
  querying.value = true
  try {
    const resp = await api.post('/v1/admin/database/query', { query: sql })
    queryResult.value = resp.data?.data || null
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('查询执行失败')))
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
    message.warning(t('请输入要优化的表名'))
    return
  }
  optimizing.value = true
  try {
    const resp = await api.post(`/v1/admin/database/optimize/${action}`, { table })
    const d = resp.data?.data || {}
    message.success(t('优化完成：{action} {table}（{status}）', { action: d.action || action, table: d.table || table, status: d.status || 'ok' }))
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('优化失败')))
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
      <h1>{{ $t('🗄️ 数据库管理') }}</h1>
      <Space>
        <Button @click="loadStatus">
          {{ $t('刷新状态') }}
        </Button>
        <Button @click="loadBackups">
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('刷新备份') }}
        </Button>
      </Space>
    </div>

    <Spin :spinning="initialLoading">
      <!-- 状态卡 -->
      <Card
        :title="$t('数据库状态')"
        style="margin-bottom: 16px"
        :loading="statusLoading"
      >
        <Descriptions
          :column="2"
          bordered
          size="small"
        >
          <Descriptions.Item :label="$t('版本')">
            <Space>
              <DatabaseOutlined />
              {{ statusData?.version || '-' }}
            </Space>
          </Descriptions.Item>
          <Descriptions.Item :label="$t('连接状态')">
            <Tag :color="statusData?.connected ? 'green' : 'red'">
              {{ statusData?.connected ? $t('已连接') : $t('未连接') }}
            </Tag>
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <!-- 配置表 -->
      <Card
        :title="$t('数据库配置')"
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
            <EmptyState :description="$t('暂无配置项')" />
          </template>
        </Table>
      </Card>

      <!-- 备份列表 -->
      <Card style="margin-bottom: 16px">
        <template #title>
          <Space>{{ $t('💾 备份列表') }}</Space>
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
            {{ $t('创建备份') }}
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
              :description="$t('暂无备份')"
              :hint="$t('点击右上角「创建备份」生成一次备份')"
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
                :title="$t('⚠️ 警告：从备份恢复将覆盖当前数据库数据，此操作不可撤销！确定继续吗？')"
                :ok-text="$t('确定恢复')"
                :cancel-text="$t('取消')"
                @confirm="restoreBackup(record)"
              >
                <Button
                  size="small"
                  danger
                  :loading="restoringName === record.name"
                >
                  {{ $t('恢复') }}
                </Button>
              </Popconfirm>
            </template>
          </template>
        </Table>
      </Card>

      <!-- SQL 查询器 -->
      <Card
        :title="$t('SQL 查询器（只读）')"
        style="margin-bottom: 16px"
      >
        <Alert
          type="info"
          show-icon
          :message="$t('仅允许只读查询（SELECT 等），不会执行任何写操作。')"
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
            {{ $t('执行查询') }}
          </Button>
          <Button @click="queryResult = null; queryText = ''">
            {{ $t('清空') }}
          </Button>
        </Space>

        <template v-if="queryResult">
          <Space
            style="margin-bottom: 8px"
            wrap
          >
            <Statistic
              :title="$t('返回行数')"
              :value="queryResult.count ?? (queryResult.rows || []).length"
            />
            <Tag
              v-if="queryResult.truncated"
              color="orange"
            >
              {{ $t('结果已截断') }}
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
              <EmptyState :description="$t('查询无返回结果')" />
            </template>
            <template #bodyCell="{ column, record }">
              <span class="cell">{{ cellText(record[(column as any).dataIndex as string]) }}</span>
            </template>
          </Table>
        </template>
        <template v-else>
          <EmptyState :description="$t('执行查询后结果将显示在这里')" />
        </template>
      </Card>

      <!-- 优化区 -->
      <Card
        :title="$t('性能优化')"
        style="margin-bottom: 16px"
      >
        <Alert
          type="warning"
          show-icon
          :message="$t('优化操作会占用数据库资源，建议在低峰期对指定表执行。')"
          style="margin-bottom: 16px"
        />
        <Space wrap>
          <Input
            v-model:value="optimizeTable"
            :placeholder="$t('输入表名，例如 users')"
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
            {{ $t('ANALYZE 更新统计信息') }}
          </Button>
          <Button
            :loading="optimizing"
            @click="runOptimize('vacuum')"
          >
            <template #icon>
              <ThunderboltOutlined />
            </template>
            {{ $t('VACUUM 回收存储空间') }}
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
