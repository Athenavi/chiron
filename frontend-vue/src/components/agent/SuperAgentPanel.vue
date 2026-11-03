<script setup lang="ts">
import { computed, markRaw, onMounted, onUnmounted, ref } from 'vue'
import {
  Alert, Button, Input, InputNumber, Modal, Select, Switch, Tag, message,
} from 'ant-design-vue'
import { ThunderboltOutlined, ClockCircleOutlined } from '@ant-design/icons-vue'
import EmptyState from '../common/EmptyState.vue'
import { serverErrorMessage } from '../../utils/apiError'
import { useI18n } from 'vue-i18n'
import {
  createSchedule, deleteSchedule, getSuperAgentSnapshot, listSchedules,
  listSelectableSessions, saveSuperAgentConfig, stopTakeover, triggerSchedule,
  updateSchedule,
} from '../../api/superagent'
import type {
  SelectableSession, SuperAgentSchedule, Takeover, TakeoverConfig,
} from '../../api/superagent'

const { t } = useI18n()

/** 节拍与窗口的上下界与后端一致（superagent.go 的 min/max 常量）。 */
const DURATION_MIN_MINUTES = 1
const DURATION_MAX_MINUTES = 1440
const SCHEDULE_LIMIT_FALLBACK = 10

const loading = ref(true)
const saving = ref(false)
const takeovers = ref<Takeover[]>([])
const sessions = ref<SelectableSession[]>([])
const schedules = ref<SuperAgentSchedule[]>([])
const scheduleLimit = ref(SCHEDULE_LIMIT_FALLBACK)

const form = ref({
  enabled: false,
  session_ids: [] as string[],
  duration_minutes: 60,
  tick_seconds: 180,
  max_rounds: 0,
  token_cap: 0,
})

const sessionOptions = computed(() => sessions.value.map(s => ({
  value: s.id,
  label: s.title?.trim() || s.id,
})))

function applyConfig(cfg: TakeoverConfig) {
  form.value = {
    enabled: cfg.enabled,
    session_ids: [...(cfg.session_ids || [])],
    duration_minutes: Math.max(
      DURATION_MIN_MINUTES,
      Math.round((cfg.window?.duration_seconds || 3600) / 60),
    ),
    tick_seconds: cfg.window?.tick_seconds || 180,
    max_rounds: cfg.window?.max_rounds ?? 0,
    token_cap: cfg.budget?.token_cap ?? 0,
  }
}

async function loadAll() {
  loading.value = true
  try {
    const [snap, sch, ses] = await Promise.all([
      getSuperAgentSnapshot(),
      listSchedules(),
      listSelectableSessions(),
    ])
    applyConfig(snap.config)
    takeovers.value = snap.takeovers || []
    schedules.value = sch.schedules
    scheduleLimit.value = sch.limit || SCHEDULE_LIMIT_FALLBACK
    sessions.value = ses
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.failed_to_load')))
  } finally {
    loading.value = false
  }
}

/** 只刷新运行态：不动表单，否则会覆盖用户正在改的输入。 */
async function refreshRuntime() {
  try {
    const snap = await getSuperAgentSnapshot()
    takeovers.value = snap.takeovers || []
  } catch {
    // 轮询失败不打扰用户，下一次照常重试
  }
}

let pollTimer: number | undefined

onMounted(async () => {
  await loadAll()
  pollTimer = window.setInterval(() => {
    if (takeovers.value.length) void refreshRuntime()
  }, 15000)
})
onUnmounted(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})

function buildPayload() {
  return {
    enabled: form.value.enabled,
    session_ids: form.value.session_ids,
    duration_seconds: form.value.duration_minutes * 60,
    token_cap: form.value.token_cap,
    tick_seconds: form.value.tick_seconds,
    max_rounds: form.value.max_rounds,
  }
}

async function persist(enable: boolean) {
  saving.value = true
  try {
    const snap = await saveSuperAgentConfig({ ...buildPayload(), enabled: enable })
    form.value.enabled = enable
    applyConfig({ ...snap.config, enabled: enable })
    takeovers.value = snap.takeovers || []
    message.success(t('agent.superagent_saved'))
  } catch (e) {
    // 失败要把开关拨回去：停在一个没生效的"已开启"上，用户会放心离开电脑。
    form.value.enabled = !enable
    message.error(serverErrorMessage(e, t('errors.save_failed')))
  } finally {
    saving.value = false
  }
}

/** Switch 的 change 回传 CheckedType（组件可配 checkedValue），这里只认严格的 true。 */
function onToggle(checked: boolean | string | number) {
  if (checked !== true) {
    // 关开关只改意图，不会停掉正在跑的接管；不说清楚，用户会以为已经停了。
    if (takeovers.value.length) {
      message.info(t('agent.superagent_stop_hint'))
    }
    void persist(false)
    return
  }
  if (form.value.session_ids.length === 0) {
    form.value.enabled = false
    message.warning(t('agent.superagent_session_required'))
    return
  }
  if (form.value.duration_minutes < form.value.tick_seconds / 60) {
    form.value.enabled = false
    message.warning(t('agent.superagent_window_too_short'))
    return
  }
  Modal.confirm({
    title: t('agent.superagent_enable_title'),
    content: t('agent.superagent_enable_text', { count: form.value.session_ids.length }),
    okText: t('common.confirm'),
    cancelText: t('common.cancel'),
    onOk: () => persist(true),
    onCancel: () => { form.value.enabled = false },
  })
}

function stopOne(item: Takeover) {
  Modal.confirm({
    title: t('agent.superagent_stop_title'),
    content: t('agent.superagent_stop_text'),
    okText: t('agent.superagent_stop'),
    okType: 'danger',
    cancelText: t('common.cancel'),
    onOk: async () => {
      try {
        await stopTakeover(item.id)
        message.success(t('agent.superagent_stopped'))
        await refreshRuntime()
      } catch (e) {
        message.error(serverErrorMessage(e, t('errors.operation_failed')))
      }
    },
  })
}

function sessionLabel(id: string): string {
  const hit = sessions.value.find(s => s.id === id)
  return hit?.title?.trim() || id
}

function fmtTime(v?: string): string {
  if (!v) return '—'
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

function statusColor(status?: string): string {
  switch (status) {
    case 'success': return 'green'
    case 'failed': return 'red'
    case 'skipped_busy': return 'default'
    default: return 'default'
  }
}

function statusText(status?: string): string {
  if (!status) return '—'
  if (status === 'skipped_busy') return t('agent.superagent_status_skipped_busy')
  // 预算耗尽是一条**终态原因**而不是状态值，但它同样需要一个可读的标签：
  // 原样透出 `budget_exhausted` 对用户毫无信息量。
  if (status === 'budget_exhausted') return t('agent.superagent_budget_exhausted_notice')
  return status
}

// ── 预算读数：`token_cap == 0` 表示不限，此时只报已花 ──

/** 已花 / 上限的展示文本。上限为 0 只报已花，不编造一个分母。 */
function spentLabel(item: Takeover): string {
  const spent = item.token_spent.toLocaleString()
  if (!item.token_cap || item.token_cap <= 0) return `${spent} tokens`
  return `${spent} / ${item.token_cap.toLocaleString()} tokens`
}

/** 已用掉 80% 以上时标红提示：接管会**自动**停，用户该提前决定加预算还是收工。 */
function budgetNearLimit(item: Takeover): boolean {
  const cap = item.token_cap
  if (!cap || cap <= 0) return false
  return item.token_spent >= cap * 0.8
}

// ── 定时任务：新建 / 编辑 ──

const editorOpen = ref(false)
const editorSaving = ref(false)
const editingId = ref('')
const editor = ref({
  name: '',
  schedule: '0 * * * *',
  window_minutes: 60,
  session_ids: [] as string[],
  enabled: false,
})

function openEditor(row?: SuperAgentSchedule) {
  editingId.value = row?.id || ''
  editor.value = {
    name: row?.name || '',
    schedule: row?.schedule || '0 * * * *',
    window_minutes: Math.max(DURATION_MIN_MINUTES, Math.round((row?.window_seconds || 3600) / 60)),
    session_ids: [...(row?.session_ids || [])],
    enabled: row?.enabled ?? false,
  }
  editorOpen.value = true
}

async function saveEditor() {
  if (!editor.value.schedule.trim()) {
    message.warning(t('agent.superagent_schedule_cron_required'))
    return
  }
  if (editor.value.session_ids.length === 0) {
    message.warning(t('agent.superagent_session_required'))
    return
  }
  editorSaving.value = true
  try {
    const payload = {
      name: editor.value.name.trim(),
      schedule: editor.value.schedule.trim(),
      window_seconds: editor.value.window_minutes * 60,
      session_ids: editor.value.session_ids,
      enabled: editor.value.enabled,
    }
    if (editingId.value) await updateSchedule(editingId.value, payload)
    else await createSchedule(payload)
    message.success(t('agent.superagent_saved'))
    editorOpen.value = false
    await reloadSchedules()
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.save_failed')))
  } finally {
    editorSaving.value = false
  }
}

async function reloadSchedules() {
  try {
    const res = await listSchedules()
    schedules.value = res.schedules
    scheduleLimit.value = res.limit || SCHEDULE_LIMIT_FALLBACK
  } catch {
    // 列表刷新失败保留旧数据，不额外打扰
  }
}

function confirmDeleteSchedule(row: SuperAgentSchedule) {
  Modal.confirm({
    title: t('agent.superagent_schedule_delete_title'),
    okText: t('common.delete'),
    okType: 'danger',
    cancelText: t('common.cancel'),
    onOk: async () => {
      try {
        await deleteSchedule(row.id)
        await reloadSchedules()
      } catch (e) {
        message.error(serverErrorMessage(e, t('errors.delete_failed')))
      }
    },
  })
}

async function runScheduleNow(row: SuperAgentSchedule) {
  try {
    const res = await triggerSchedule(row.id)
    // opened=0 说明目标会话都已有接管在跑，不是失败 —— 如实说出来。
    if (res.opened > 0) message.success(t('agent.superagent_triggered', { count: res.opened }))
    else message.info(t('agent.superagent_triggered_none'))
    await reloadSchedules()
    await refreshRuntime()
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.operation_failed')))
  }
}
</script>

<template>
  <div class="sa-panel">
    <div class="sa-head">
      <div>
        <h2 class="sa-title">
          {{ $t('agent.superagent_title') }}
        </h2>
        <p class="sa-sub">
          {{ $t('agent.superagent_sub') }}
        </p>
      </div>
      <Switch
        :checked="form.enabled"
        :loading="saving"
        :disabled="loading"
        @change="onToggle"
      />
    </div>

    <!-- 常驻提示：费用是这个功能最容易被低估的代价，默认态也要看得见 -->
    <Alert
      type="warning"
      show-icon
      class="sa-alert"
      :message="$t('agent.superagent_cost_warning')"
      :description="$t('agent.superagent_disabled_hint')"
    />

    <div class="sa-section">
      <label class="sa-label">{{ $t('agent.superagent_session_label') }}</label>
      <Select
        v-model:value="form.session_ids"
        mode="multiple"
        :options="sessionOptions"
        :placeholder="$t('agent.superagent_session_placeholder')"
        :loading="loading"
        allow-clear
        show-search
        option-filter-prop="label"
        class="sa-select"
      />
    </div>

    <div class="sa-grid">
      <div class="sa-field">
        <label class="sa-label">{{ $t('agent.superagent_duration_label') }}</label>
        <InputNumber
          v-model:value="form.duration_minutes"
          :min="DURATION_MIN_MINUTES"
          :max="DURATION_MAX_MINUTES"
          class="sa-number"
        />
      </div>
      <div class="sa-field">
        <label class="sa-label">{{ $t('agent.superagent_tick_label') }}</label>
        <InputNumber
          v-model:value="form.tick_seconds"
          :min="60"
          :max="3600"
          :step="30"
          class="sa-number"
        />
      </div>
      <div class="sa-field">
        <label class="sa-label">{{ $t('agent.superagent_max_rounds_label') }}</label>
        <InputNumber
          v-model:value="form.max_rounds"
          :min="0"
          class="sa-number"
        />
      </div>
      <div class="sa-field">
        <label class="sa-label">{{ $t('agent.superagent_token_cap_label') }}</label>
        <InputNumber
          v-model:value="form.token_cap"
          :min="0"
          :step="10000"
          class="sa-number"
        />
      </div>
    </div>

    <div class="sa-section">
      <label class="sa-label">{{ $t('agent.superagent_active_title') }}</label>
      <EmptyState
        v-if="!takeovers.length"
        size="list"
        :icon="markRaw(ThunderboltOutlined)"
        :description="$t('agent.superagent_empty_takeovers')"
      />
      <div
        v-for="item in takeovers"
        :key="item.id"
        class="sa-takeover"
      >
        <div class="sa-takeover-main">
          <div class="sa-takeover-title">
            {{ sessionLabel(item.session_id) }}
            <Tag
              :color="item.status === 'active' ? 'blue' : 'orange'"
              class="sa-tag"
            >
              {{ item.status }}
            </Tag>
            <Tag
              v-if="item.last_status"
              :color="statusColor(item.last_status)"
              class="sa-tag"
            >
              {{ statusText(item.last_status) }}
            </Tag>
          </div>
          <div class="sa-takeover-meta">
            <ClockCircleOutlined />
            {{ $t('agent.superagent_active_ends') }}: {{ fmtTime(item.ends_at) }}
            <!-- 已花 / 上限：只写 spent 时，用户看不出"离停还有多远"，
                 而预算耗尽会**自动**停掉接管 —— 那是需要提前预判的事。 -->
            <span
              class="sa-spent"
              :class="{ 'sa-spent--near': budgetNearLimit(item) }"
            >· {{ spentLabel(item) }}</span>
          </div>
        </div>
        <Button
          danger
          size="small"
          :disabled="item.status !== 'active'"
          @click="stopOne(item)"
        >
          {{ $t('agent.superagent_stop') }}
        </Button>
      </div>
    </div>

    <!-- ── 定时任务 ── -->
    <div class="sa-head sa-head-schedules">
      <div>
        <h3 class="sa-subtitle">
          {{ $t('agent.superagent_schedules_title') }}
        </h3>
        <p class="sa-sub">
          {{ schedules.length }} / {{ scheduleLimit }}
        </p>
      </div>
      <Button
        type="primary"
        :disabled="schedules.length >= scheduleLimit"
        @click="openEditor()"
      >
        {{ $t('agent.superagent_schedule_new') }}
      </Button>
    </div>

    <EmptyState
      v-if="!schedules.length"
      size="list"
      :icon="markRaw(ClockCircleOutlined)"
      :description="$t('common.empty')"
    />
    <div
      v-for="row in schedules"
      :key="row.id"
      class="sa-schedule"
    >
      <div class="sa-takeover-main">
        <div class="sa-takeover-title">
          {{ row.name || row.schedule }}
          <Tag :color="row.enabled ? 'green' : 'default'">
            {{ row.enabled ? 'on' : 'off' }}
          </Tag>
        </div>
        <div class="sa-takeover-meta">
          <code class="sa-cron">{{ row.schedule }}</code>
          · {{ Math.round(row.window_seconds / 60) }}min
          · {{ $t('agent.superagent_schedule_last_run') }}: {{ fmtTime(row.last_run_at) }}
          <Tag
            v-if="row.last_status"
            :color="statusColor(row.last_status)"
            class="sa-tag"
          >
            {{ statusText(row.last_status) }}
          </Tag>
        </div>
      </div>
      <div class="sa-actions">
        <Button
          size="small"
          @click="runScheduleNow(row)"
        >
          {{ $t('agent.superagent_schedule_trigger') }}
        </Button>
        <Button
          size="small"
          @click="openEditor(row)"
        >
          {{ $t('common.edit') }}
        </Button>
        <Button
          size="small"
          danger
          @click="confirmDeleteSchedule(row)"
        >
          {{ $t('common.delete') }}
        </Button>
      </div>
    </div>

    <Modal
      :open="editorOpen"
      :title="editingId ? $t('agent.superagent_schedule_edit') : $t('agent.superagent_schedule_new')"
      :confirm-loading="editorSaving"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      @ok="saveEditor"
      @cancel="editorOpen = false"
    >
      <div class="sa-field">
        <label class="sa-label">{{ $t('common.name') }}</label>
        <Input
          v-model:value="editor.name"
          :maxlength="128"
        />
      </div>
      <div class="sa-field">
        <label class="sa-label">{{ $t('agent.superagent_schedule_cron') }}</label>
        <Input
          v-model:value="editor.schedule"
          placeholder="0 * * * *"
        />
      </div>
      <div class="sa-field">
        <label class="sa-label">{{ $t('agent.superagent_schedule_window') }}</label>
        <InputNumber
          v-model:value="editor.window_minutes"
          :min="DURATION_MIN_MINUTES"
          :max="DURATION_MAX_MINUTES"
          class="sa-number"
        />
      </div>
      <div class="sa-field">
        <label class="sa-label">{{ $t('agent.superagent_session_label') }}</label>
        <Select
          v-model:value="editor.session_ids"
          mode="multiple"
          :options="sessionOptions"
          allow-clear
          show-search
          option-filter-prop="label"
          class="sa-select"
        />
      </div>
      <div class="sa-field sa-inline">
        <label class="sa-label">{{ $t('common.enable') }}</label>
        <Switch v-model:checked="editor.enabled" />
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.sa-panel { display: flex; flex-direction: column; gap: 18px; }
.sa-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.sa-head-schedules { margin-top: 8px; padding-top: 18px; border-top: var(--hairline-width) solid var(--border-card); }
.sa-title { margin: 0; font-size: 17px; font-weight: 600; }
.sa-subtitle { margin: 0; font-size: 15px; font-weight: 600; }
.sa-sub { margin: 4px 0 0; color: var(--text-tertiary); font-size: 13px; }
.sa-alert { border-radius: var(--radius-lg); }
.sa-section { display: flex; flex-direction: column; gap: 8px; }
.sa-label { font-size: 13px; color: var(--text-tertiary); }
.sa-select { width: 100%; }
.sa-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 14px; }
.sa-field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
.sa-inline { flex-direction: row; align-items: center; gap: 12px; }
.sa-number { width: 100%; }
.sa-takeover, .sa-schedule {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 12px 14px;
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
}
.sa-schedule { margin-bottom: 8px; }
.sa-takeover-main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.sa-takeover-title { display: flex; align-items: center; gap: 8px; font-weight: 500; }
.sa-takeover-meta { color: var(--text-tertiary); font-size: 12px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.sa-tag { margin-inline-start: 4px; }
.sa-spent { color: var(--text-tertiary); }
.sa-spent--near { color: var(--warning, #d97706); font-weight: 600; }
.sa-cron { font-family: var(--font-mono, monospace); font-size: 12px; }
.sa-actions { display: flex; gap: 8px; flex-shrink: 0; }
</style>
