<script setup lang="ts">
import { ref, onMounted, onUnmounted, h } from 'vue'
import { Card, Table, Tooltip, Tag, Button, Input, Modal, Space, Popconfirm, Switch, message } from 'ant-design-vue'
import {
  ClockCircleOutlined, PlusOutlined, ReloadOutlined,
  PlayCircleOutlined, CopyOutlined, EditOutlined, DeleteOutlined,
} from '@ant-design/icons-vue'
import { api, triggerCronJob, cronWebhookUrl } from '@/api'
import EmptyState from '@/components/common/EmptyState.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
/**
 * 定时任务管理（从 DashboardView 拆出）。
 *
 * 职责归位：定时任务是配置类操作（CRUD + 手动触发 + Webhook），不属于「仪表盘」的
 * 只读概览定位；此前它占据 Dashboard 模板约 177 行 + 大量脚本逻辑，使仪表盘职责混杂。
 */

// ── 定时任务（Cron Jobs）：列表 + 手动触发 + Webhook + 自动刷新 ──
interface CronJob {
  id: string
  name: string
  schedule: string
  task: string
  enabled: boolean
  last_run_at: string | null
  last_status: string
  webhook_token?: string
  created_at: string
}

const cronJobs = ref<CronJob[]>([])
const cronLoading = ref(false)
const cronRefreshing = ref(false)
let cronAutoTimer: ReturnType<typeof setInterval> | null = null

/** 拉取定时任务列表（silent：静默刷新，不闪 loading / 不弹错） */
async function fetchCronJobs(silent = false) {
  if (cronRefreshing.value) return
  cronRefreshing.value = true
  if (!silent) cronLoading.value = true
  try {
    const res = await api.get('/v1/admin/cron-jobs')
    const d = res.data?.data || res.data
    cronJobs.value = Array.isArray(d?.jobs) ? d.jobs : []
  } catch {
    if (!silent) message.error(t('定时任务列表加载失败'))
  } finally {
    cronLoading.value = false
    cronRefreshing.value = false
  }
}

/** 手动触发：POST /v1/admin/cron-jobs/{id}/trigger → 成功提示 + 刷新列表 */
async function triggerCronJobById(job: CronJob) {
  try {
    await triggerCronJob(job.id)
    message.success(t('已触发「{name}」，任务将异步执行', { name: job.name }))
    await fetchCronJobs(true)
  } catch (e: any) {
    message.error(t('触发失败: {error}', { error: e?.response?.data?.error || e?.message || t('网络错误') }))
  }
}

/** 复制文本：优先 Clipboard API，失败降级 execCommand */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      return ok
    } catch {
      return false
    }
  }
}

/** 复制 Webhook 触发地址（仅列表含 webhook_token 时展示） */
async function copyCronWebhook(job: CronJob) {
  if (!job.webhook_token) return
  const ok = await copyText(cronWebhookUrl(job.id, job.webhook_token))
  if (ok) message.success(t('Webhook 地址已复制'))
  else message.error(t('复制失败，请手动复制'))
}

/** last_status → Tag 颜色：成功绿 / 失败红 / 其余灰（pending/未运行） */
function cronStatusColor(s: string): string {
  if (s === 'success') return 'success'
  if (s === 'failed' || s === 'error') return 'error'
  return 'default'
}
function cronStatusLabel(s: string): string {
  if (!s) return t('未运行')
  if (s === 'success') return t('成功')
  if (s === 'failed' || s === 'error') return t('失败')
  return s
}

function formatCronTime(ts: string | null): string {
  if (!ts) return '—'
  const d = new Date(ts)
  if (isNaN(d.getTime())) return '—'
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}

// ── 创建 / 编辑 ──
const cronModalOpen = ref(false)
const cronEditing = ref<CronJob | null>(null)
const cronSaving = ref(false)
const cronForm = ref({ name: '', schedule: '', task: '', enabled: true })

function openCronCreate() {
  cronEditing.value = null
  cronForm.value = { name: '', schedule: '0 9 * * *', task: '', enabled: true }
  cronModalOpen.value = true
}

function openCronEdit(job: CronJob) {
  cronEditing.value = job
  cronForm.value = { name: job.name, schedule: job.schedule, task: job.task, enabled: job.enabled }
  cronModalOpen.value = true
}

async function saveCronJob() {
  const f = cronForm.value
  if (!f.name.trim() || !f.schedule.trim() || !f.task.trim()) {
    message.warning(t('名称、Cron 表达式与任务 JSON 均为必填'))
    return
  }
  try {
    JSON.parse(f.task)
  } catch {
    message.error(t('任务 JSON 格式不正确，请检查后重试'))
    return
  }
  cronSaving.value = true
  try {
    if (cronEditing.value) {
      await api.put(`/v1/admin/cron-jobs/${cronEditing.value.id}`, f)
      message.success(t('已更新定时任务'))
    } else {
      const res = await api.post('/v1/admin/cron-jobs', f)
      const d = res.data?.data || res.data
      message.success(t('已创建定时任务'))
      // 创建响应含 webhook_token：顺手复制一次触发地址（列表刷新后行内仍可复制）
      if (d?.id && d?.webhook_token) {
        const ok = await copyText(cronWebhookUrl(d.id, d.webhook_token))
        if (ok) message.success(t('Webhook 触发地址已复制到剪贴板'))
      }
    }
    cronModalOpen.value = false
    await fetchCronJobs()
  } catch (e: any) {
    message.error(t('保存失败: {error}', { error: e?.response?.data?.error || e?.message || t('网络错误') }))
  } finally {
    cronSaving.value = false
  }
}

async function deleteCronJob(job: CronJob) {
  try {
    await api.delete(`/v1/admin/cron-jobs/${job.id}`)
    message.success(t('已删除定时任务'))
    await fetchCronJobs(true)
  } catch (e: any) {
    message.error(t('删除失败: {error}', { error: e?.response?.data?.error || e?.message || t('网络错误') }))
  }
}

const cronColumns = [
  { title: t('名称'), dataIndex: 'name', key: 'name', width: 170, ellipsis: true },
  { title: t('计划'), dataIndex: 'schedule', key: 'schedule', width: 120 },
  { title: t('任务'), dataIndex: 'task', key: 'task', ellipsis: true },
  { title: t('启用'), dataIndex: 'enabled', key: 'enabled', width: 64 },
  { title: t('上次运行'), dataIndex: 'last_run_at', key: 'last_run_at', width: 130 },
  { title: t('状态'), dataIndex: 'last_status', key: 'last_status', width: 84 },
  { title: t('操作'), key: 'actions', width: 300 },
]

onMounted(() => {
  fetchCronJobs()
  // 自动刷新：每 30s 静默刷新（last_status / last_run_at 会随执行更新）
  cronAutoTimer = setInterval(() => { fetchCronJobs(true) }, 30_000)
})

onUnmounted(() => {
  if (cronAutoTimer) { clearInterval(cronAutoTimer); cronAutoTimer = null }
})
</script>

<template>
  <div class="cron-page">
    <Card
      class="cron-card"
      :bordered="false"
    >
      <template #title>
        <span class="chart-title">
          <ClockCircleOutlined class="chart-title-icon" /> {{ $t('定时任务') }}
        </span>
      </template>
      <template #extra>
        <Space :size="8">
          <Button
            size="small"
            :icon="h(ReloadOutlined)"
            :loading="cronRefreshing"
            @click="fetchCronJobs()"
          >
            {{ $t('立即刷新') }}
          </Button>
          <Button
            size="small"
            type="primary"
            :icon="h(PlusOutlined)"
            @click="openCronCreate"
          >
            {{ $t('新建任务') }}
          </Button>
        </Space>
      </template>
      <EmptyState
        v-if="!cronLoading && cronJobs.length === 0"
        size="list"
        :description="$t('暂无定时任务')"
        :hint="$t('点击「新建任务」创建定时自动化任务')"
      />
      <Table
        v-else
        :columns="cronColumns"
        :data-source="cronJobs"
        :loading="cronLoading"
        :pagination="{ pageSize: 8, showSizeChanger: false, showTotal: (n: number) => t('共 {n} 条', { n }) }"
        size="small"
        row-key="id"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'schedule'">
            <code class="cron-schedule">{{ record.schedule }}</code>
          </template>
          <template v-else-if="column.key === 'task'">
            <Tooltip
              :title="record.task"
              placement="topLeft"
            >
              <span class="cron-task">{{ record.task }}</span>
            </Tooltip>
          </template>
          <template v-else-if="column.key === 'enabled'">
            <Switch
              :checked="record.enabled"
              size="small"
              disabled
            />
          </template>
          <template v-else-if="column.key === 'last_run_at'">
            {{ formatCronTime(record.last_run_at) }}
          </template>
          <template v-else-if="column.key === 'last_status'">
            <Tag :color="cronStatusColor(record.last_status)">
              {{ cronStatusLabel(record.last_status) }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'actions'">
            <Space
              :size="0"
              wrap
            >
              <Button
                size="small"
                type="link"
                :icon="h(PlayCircleOutlined)"
                @click="triggerCronJobById(record as CronJob)"
              >
                {{ $t('手动触发') }}
              </Button>
              <Button
                v-if="record.webhook_token"
                size="small"
                type="link"
                :icon="h(CopyOutlined)"
                @click="copyCronWebhook(record as CronJob)"
              >
                {{ $t('复制 Webhook') }}
              </Button>
              <Button
                size="small"
                type="link"
                :icon="h(EditOutlined)"
                @click="openCronEdit(record as CronJob)"
              >
                {{ $t('编辑') }}
              </Button>
              <Popconfirm
                :title="$t('确定删除该定时任务？')"
                :ok-text="$t('删除')"
                :cancel-text="$t('取消')"
                @confirm="deleteCronJob(record as CronJob)"
              >
                <Button
                  size="small"
                  type="link"
                  danger
                  :icon="h(DeleteOutlined)"
                >
                  {{ $t('删除') }}
                </Button>
              </Popconfirm>
            </Space>
          </template>
        </template>
      </Table>
    </Card>

    <!-- 新建 / 编辑定时任务对话框 -->
    <Modal
      :open="cronModalOpen"
      :title="cronEditing ? $t('编辑定时任务') : $t('新建定时任务')"
      :confirm-loading="cronSaving"
      :ok-text="$t('保存')"
      :cancel-text="$t('取消')"
      @ok="saveCronJob"
      @cancel="cronModalOpen = false"
    >
      <div class="cron-form">
        <div class="cron-field">
          <label class="cron-label">{{ $t('任务名称') }}</label>
          <Input
            v-model:value="cronForm.name"
            :placeholder="$t('例如：每日早报生成')"
            :maxlength="120"
          />
        </div>
        <div class="cron-field">
          <label class="cron-label">{{ $t('Cron 表达式') }}</label>
          <Input
            v-model:value="cronForm.schedule"
            :placeholder="$t('例如：0 9 * * *（每天 09:00）')"
          />
          <div class="cron-hint">
            {{ $t('标准 5 段 Cron：分 时 日 月 周') }}
          </div>
        </div>
        <div class="cron-field">
          <label class="cron-label">{{ $t('任务 JSON') }}</label>
          <Input.TextArea
            v-model:value="cronForm.task"
            :rows="4"
            class="cron-task-input"
            placeholder="{&quot;type&quot;:&quot;agent&quot;,&quot;agent_id&quot;:&quot;...&quot;,&quot;prompt&quot;:&quot;...&quot;}"
          />
          <div class="cron-hint">
            {{ $t('任务类型示例：') }}<br>
            <code>{"type":"agent","agent_id":"...","prompt":"..."}</code>{{ $t('（Agent 任务）') }}<br>
            <code>{"type":"quick","user_input":"...","mode":"auto"}</code>{{ $t('（统一任务）') }}
          </div>
        </div>
        <div class="cron-field cron-field-row">
          <label class="cron-label">{{ $t('启用') }}</label>
          <Switch v-model:checked="cronForm.enabled" />
        </div>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.cron-page { display: flex; flex-direction: column; gap: 16px; }

/* ── 定时任务卡片 ── */
.cron-card {
  border-radius: 10px !important;
  background: var(--bg-card) !important;
  box-shadow: var(--shadow-md);
}
.cron-card :deep(.ant-card-head) {
  min-height: 44px;
  border-bottom: 1px solid var(--border-card);
  padding: 0 16px;
}
.cron-card :deep(.ant-card-body) { padding: 4px 16px 16px; }
.cron-card :deep(.ant-table) { background: transparent; }
.cron-card :deep(.ant-table-thead > tr > th) {
  background: var(--bg-secondary);
  font-size: 12px;
  font-weight: 600;
  color: var(--text-tertiary);
  border-bottom: 1px solid var(--border-card);
}
.cron-card :deep(.ant-table-tbody > tr > td) {
  border-bottom: 1px solid var(--border-card);
  font-size: 13px;
}
.cron-card :deep(.ant-table-tbody > tr:hover > td) {
  background: var(--bg-hover) !important;
}
.cron-schedule {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-secondary);
  padding: 1px 6px;
  border-radius: 4px;
}
.cron-task {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-secondary);
}
.chart-title { display: inline-flex; align-items: center; gap: 6px; font-size: 14px; font-weight: 600; }
.chart-title-icon { color: var(--primary); }

/* ── 新建/编辑对话框 ── */
.cron-form { display: flex; flex-direction: column; gap: 12px; }
.cron-field { display: flex; flex-direction: column; gap: 4px; }
.cron-field-row { flex-direction: row; align-items: center; gap: 8px; }
.cron-label { font-size: 12px; font-weight: 600; color: var(--text-secondary); }
.cron-hint { font-size: 12px; color: var(--text-tertiary); line-height: 1.7; }
.cron-hint code {
  font-family: var(--font-mono);
  font-size: 11px;
  background: var(--bg-secondary);
  padding: 1px 5px;
  border-radius: 4px;
}
</style>
