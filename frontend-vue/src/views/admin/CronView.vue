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
    if (!silent) message.error(t('errors.failed_to_load_scheduled_tasks'))
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
    message.error(t('触发失败: {error}', { error: e?.response?.data?.error || e?.message || t('errors.network_error_2') }))
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
  if (ok) message.success(t('common.webhook_url_copied'))
  else message.error(t('errors.copy_failed_please_copy_manually'))
}

/** last_status → Tag 颜色：成功绿 / 失败红 / 其余灰（pending/未运行） */
function cronStatusColor(s: string): string {
  if (s === 'success') return 'success'
  if (s === 'failed' || s === 'error') return 'error'
  return 'default'
}
function cronStatusLabel(s: string): string {
  if (!s) return t('common.not_running')
  if (s === 'success') return t('common.success')
  if (s === 'failed' || s === 'error') return t('errors.failed')
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
    message.warning(t('errors.name_cron_expression_and_task_json_are_all_required'))
    return
  }
  try {
    JSON.parse(f.task)
  } catch {
    message.error(t('errors.task_json_format_is_incorrect_please_check_and_retry'))
    return
  }
  cronSaving.value = true
  try {
    if (cronEditing.value) {
      await api.put(`/v1/admin/cron-jobs/${cronEditing.value.id}`, f)
      message.success(t('workflow.scheduled_task_updated'))
    } else {
      const res = await api.post('/v1/admin/cron-jobs', f)
      const d = res.data?.data || res.data
      message.success(t('workflow.scheduled_task_created'))
      // 创建响应含 webhook_token：顺手复制一次触发地址（列表刷新后行内仍可复制）
      if (d?.id && d?.webhook_token) {
        const ok = await copyText(cronWebhookUrl(d.id, d.webhook_token))
        if (ok) message.success(t('workflow.webhook_trigger_url_copied_to_clipboard'))
      }
    }
    cronModalOpen.value = false
    await fetchCronJobs()
  } catch (e: any) {
    message.error(t('保存失败: {error}', { error: e?.response?.data?.error || e?.message || t('errors.network_error_2') }))
  } finally {
    cronSaving.value = false
  }
}

async function deleteCronJob(job: CronJob) {
  try {
    await api.delete(`/v1/admin/cron-jobs/${job.id}`)
    message.success(t('workflow.scheduled_task_deleted'))
    await fetchCronJobs(true)
  } catch (e: any) {
    message.error(t('删除失败: {error}', { error: e?.response?.data?.error || e?.message || t('errors.network_error_2') }))
  }
}

const cronColumns = [
  { title: t('common.name'), dataIndex: 'name', key: 'name', width: 170, ellipsis: true },
  { title: t('billing.plan'), dataIndex: 'schedule', key: 'schedule', width: 120 },
  { title: t('workflow.task_2'), dataIndex: 'task', key: 'task', ellipsis: true },
  { title: t('common.enable'), dataIndex: 'enabled', key: 'enabled', width: 64 },
  { title: t('common.last_run'), dataIndex: 'last_run_at', key: 'last_run_at', width: 130 },
  { title: t('common.status'), dataIndex: 'last_status', key: 'last_status', width: 84 },
  { title: t('common.action'), key: 'actions', width: 300 },
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
          <ClockCircleOutlined class="chart-title-icon" /> {{ $t('workflow.scheduled_tasks') }}
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
            {{ $t('common.refresh_now') }}
          </Button>
          <Button
            size="small"
            type="primary"
            :icon="h(PlusOutlined)"
            @click="openCronCreate"
          >
            {{ $t('workflow.new_task') }}
          </Button>
        </Space>
      </template>
      <EmptyState
        v-if="!cronLoading && cronJobs.length === 0"
        size="list"
        :description="$t('workflow.no_scheduled_tasks_yet')"
        :hint="$t('workflow.click_new_task_to_create_a_scheduled_automation')"
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
                {{ $t('workflow.manual_trigger') }}
              </Button>
              <Button
                v-if="record.webhook_token"
                size="small"
                type="link"
                :icon="h(CopyOutlined)"
                @click="copyCronWebhook(record as CronJob)"
              >
                {{ $t('common.copy_webhook') }}
              </Button>
              <Button
                size="small"
                type="link"
                :icon="h(EditOutlined)"
                @click="openCronEdit(record as CronJob)"
              >
                {{ $t('common.edit_2') }}
              </Button>
              <Popconfirm
                :title="$t('workflow.confirm_deleting_this_scheduled_task')"
                :ok-text="$t('common.delete')"
                :cancel-text="$t('common.cancel')"
                @confirm="deleteCronJob(record as CronJob)"
              >
                <Button
                  size="small"
                  type="link"
                  danger
                  :icon="h(DeleteOutlined)"
                >
                  {{ $t('common.delete') }}
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
      :title="cronEditing ? $t('workflow.edit_scheduled_task') : $t('workflow.new_scheduled_task')"
      :confirm-loading="cronSaving"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      @ok="saveCronJob"
      @cancel="cronModalOpen = false"
    >
      <div class="cron-form">
        <div class="cron-field">
          <label class="cron-label">{{ $t('workflow.task_name') }}</label>
          <Input
            v-model:value="cronForm.name"
            :placeholder="$t('common.e_g_daily_morning_report_generation')"
            :maxlength="120"
          />
        </div>
        <div class="cron-field">
          <label class="cron-label">{{ $t('common.cron_expression') }}</label>
          <Input
            v-model:value="cronForm.schedule"
            :placeholder="$t('common.e_g_0_9_every_day_at_09_00')"
          />
          <div class="cron-hint">
            {{ $t('common.standard_5_field_cron_minute_hour_day_month_weekday') }}
          </div>
        </div>
        <div class="cron-field">
          <label class="cron-label">{{ $t('workflow.task_json') }}</label>
          <Input.TextArea
            v-model:value="cronForm.task"
            :rows="4"
            class="cron-task-input"
            placeholder="{&quot;type&quot;:&quot;agent&quot;,&quot;agent_id&quot;:&quot;...&quot;,&quot;prompt&quot;:&quot;...&quot;}"
          />
          <div class="cron-hint">
            {{ $t('workflow.task_type_examples') }}<br>
            <code>{"type":"agent","agent_id":"...","prompt":"..."}</code>{{ $t('workflow.agent_task') }}<br>
            <code>{"type":"quick","user_input":"...","mode":"auto"}</code>{{ $t('workflow.unified_task_2') }}
          </div>
        </div>
        <div class="cron-field cron-field-row">
          <label class="cron-label">{{ $t('common.enable') }}</label>
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
