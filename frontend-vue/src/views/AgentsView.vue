<script setup lang="ts">
import { ref, onMounted, onUnmounted, markRaw } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Button, Tabs, TabPane, Modal, Input, InputNumber, Switch, Tag,
  Alert, Dropdown, Menu, MenuItem, Select, message,
} from 'ant-design-vue'
import {
  PlusOutlined, PlayCircleOutlined, EditOutlined, DeleteOutlined,
  StopOutlined, ClockCircleOutlined, CheckCircleOutlined,
  CloseCircleOutlined, SyncOutlined, RobotOutlined, HistoryOutlined,
  MessageOutlined, ShopOutlined, TeamOutlined, LockOutlined,
} from '@ant-design/icons-vue'
import {
  listAgents, createAgent, updateAgent, deleteAgent,
  runAgent, listAgentSessions, getAgentSession,
  listMarket, installMarket, setAgentVisibility,
} from '../api'
import type { Agent, AgentSession, MarketItem, WorkbenchResource } from '../api'
import { listKnowledgeBases, listPlugins, listSkillResources, listWorkflows } from '../api'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import SkillMarketCard from '../components/SkillMarketCard.vue'
import ToolPicker from '../components/agent/ToolPicker.vue'
import SaveToKnowledgeDialog from '../components/chat/SaveToKnowledgeDialog.vue'
import { setChatPrefill } from '../components/chat/chatPrefill'
import { agentResultToMarkdown, parseAgentResult } from '../utils/agentResultMarkdown'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
// ── 数据 ──
// 后端列表项可能带 visibility（'private'|'tenant'）；缺失视为 private
type AgentRow = Agent & { visibility?: 'private' | 'tenant' }
const agents = ref<AgentRow[]>([])
const loadingAgents = ref(true)
const errorAgents = ref(false)
const sessions = ref<AgentSession[]>([])
const loadingSessions = ref(false)
const errorSessions = ref(false)
const activeTab = ref('agents')
const router = useRouter()
const route = useRoute()

async function loadAgents() {
  loadingAgents.value = true
  errorAgents.value = false
  try {
    agents.value = await listAgents()
  } catch {
    errorAgents.value = true
    message.error(t('获取 Agent 列表失败'))
  } finally {
    loadingAgents.value = false
  }
}

async function loadSessions() {
  loadingSessions.value = true
  errorSessions.value = false
  try {
    sessions.value = await listAgentSessions()
  } catch {
    errorSessions.value = true
    message.error(t('获取运行记录失败'))
  } finally {
    loadingSessions.value = false
  }
}

/**
 * 跨台活动直达：`?session=<id>` 时直接打开那一次运行的结果。
 *
 * 「最近活动」的 route 现在带 session id（internal/api/activities.go），点 Agent
 * 记录会落到这里 —— 以前只到列表页，用户还得自己翻。
 */
async function openSessionFromQuery() {
  const sid = typeof route.query.session === 'string' ? route.query.session.trim() : ''
  if (!sid) return
  try {
    detailSession.value = await getAgentSession(sid)
    detailOpen.value = true
  } catch {
    // 找不到（已删 / 非本人）就留在列表页，不打扰
  }
}

onMounted(() => {
  loadAgents()
  loadSessions()
  loadMarket()
  void openSessionFromQuery()
})
onUnmounted(() => stopPolling())

// ── 市场（Agent 市场：安装后出现在「我的 Agent」列表）──
const marketItems = ref<MarketItem[]>([])
const marketLoading = ref(false)
const marketError = ref(false)
const marketInstallingId = ref<string | null>(null)

async function loadMarket() {
  marketLoading.value = true
  marketError.value = false
  try {
    marketItems.value = await listMarket('agent')
  } catch {
    marketError.value = true
    message.error(t('获取 Agent 市场失败'))
  } finally {
    marketLoading.value = false
  }
}

async function handleMarketInstall(item: MarketItem) {
  marketInstallingId.value = item.id
  try {
    await installMarket('agent', item.id)
    message.success(t('「{name}」已安装', { name: item.name }))
    await Promise.all([loadMarket(), loadAgents()])
  } catch (e: any) {
    const raw = e?.response?.data
    message.error(t('安装失败: {error}', { error: raw?.message || raw?.detail || raw?.error || e?.message || '' }))
  } finally {
    marketInstallingId.value = null
  }
}

// ── 新建 / 编辑 ──
interface EditorState {
  id: string
  name: string
  description: string
  system_prompt: string
  tools_text: string
  model: string
  max_tokens: number
  temperature: number
  max_turns: number
  timeout_seconds: number
  enabled: boolean
}
const editorOpen = ref(false)
const editorSaving = ref(false)
const editingId = ref('')
const form = ref<EditorState>({
  id: '', name: '', description: '', system_prompt: '',
  tools_text: '[]', model: 'deepseek-chat', max_tokens: 4096,
  temperature: 0.6, max_turns: 5, timeout_seconds: 120, enabled: true,
})

// ── 工作台绑定：Agent 自带的知识库 / 技能 / 插件 / 工作流（派发时由引擎消费）──
// 单独用 ref 而不塞进 EditorState：它们只在编辑器里维护，不该影响既有字段与校验。
const formKbId = ref<string | undefined>(undefined)
const formSkills = ref<string[]>([])
const formPlugins = ref<string[]>([])
const formWorkflows = ref<string[]>([])
const kbOptions = ref<WorkbenchResource[]>([])
const skillOptions = ref<WorkbenchResource[]>([])
const pluginOptions = ref<WorkbenchResource[]>([])
const workflowOptions = ref<WorkbenchResource[]>([])
const bindingLoading = ref(false)

async function loadBindingOptions() {
  if (
    bindingLoading.value
    || (kbOptions.value.length && skillOptions.value.length
      && pluginOptions.value.length && workflowOptions.value.length)
  ) return
  bindingLoading.value = true
  try {
    // 四个接口互不依赖：任一失败只让那一类为空，不拖垮编辑器
    const [bases, skills, plugins, workflows] = await Promise.all([
      listKnowledgeBases().catch(() => [] as WorkbenchResource[]),
      listSkillResources().catch(() => [] as WorkbenchResource[]),
      listPlugins().catch(() => [] as WorkbenchResource[]),
      listWorkflows().catch(() => [] as WorkbenchResource[]),
    ])
    kbOptions.value = bases
    skillOptions.value = skills
    pluginOptions.value = plugins
    workflowOptions.value = workflows
  } finally {
    bindingLoading.value = false
  }
}

function resetBinding() {
  formKbId.value = undefined
  formSkills.value = []
  formPlugins.value = []
  formWorkflows.value = []
}

function openCreate() {
  editingId.value = ''
  form.value = {
    id: '', name: '', description: '', system_prompt: '',
    tools_text: '[]', model: 'deepseek-chat', max_tokens: 4096,
    temperature: 0.6, max_turns: 5, timeout_seconds: 120, enabled: true,
  }
  resetBinding()
  void loadBindingOptions()
  editorOpen.value = true
}

function openEdit(a: Agent) {
  editingId.value = a.id
  const llm = a.llm_config || {}
  form.value = {
    id: a.id,
    name: a.name,
    description: a.description || '',
    system_prompt: a.system_prompt || '',
    tools_text: JSON.stringify(a.tools || [], null, 2),
    model: String(llm.model || 'deepseek-chat'),
    max_tokens: Number(llm.max_tokens || 4096),
    temperature: Number(llm.temperature || 0.6),
    max_turns: a.max_turns || 5,
    timeout_seconds: a.timeout_seconds || 120,
    enabled: a.enabled,
  }
  // 回填工作台绑定（后端未升级时字段为 undefined，视为未绑定）
  //
  // ⚠️ workflows 必须一起回填：saveEditor 提交的是全量绑定字段，漏回填就等于提交
  // `workflows: []`，而 Go 侧 UPDATE 对空数组不判空（`len(body.Workflows) > 0` 在
  // `[]` 上为真）—— 编辑一次 Agent 就会把它已装配的工作流清空。
  formKbId.value = a.kb_id || undefined
  formSkills.value = Array.isArray(a.skills) ? [...a.skills] : []
  formPlugins.value = Array.isArray(a.plugins) ? [...a.plugins] : []
  formWorkflows.value = Array.isArray(a.workflows) ? [...a.workflows] : []
  void loadBindingOptions()
  editorOpen.value = true
}

function parseToolsText(): any[] | null {
  try {
    const v = JSON.parse(form.value.tools_text)
    return Array.isArray(v) ? v : null
  } catch {
    message.error(t('tools 不是合法的 JSON 数组'))
    return null
  }
}

async function saveEditor() {
  const f = form.value
  if (!f.name.trim()) { message.warning(t('请填写名称')); return }
  const tools = parseToolsText()
  if (tools === null) return
  const body: Partial<Agent> = {
    name: f.name.trim(),
    description: f.description,
    system_prompt: f.system_prompt,
    tools,
    llm_config: { model: f.model || 'deepseek-chat', max_tokens: f.max_tokens, temperature: f.temperature },
    max_turns: f.max_turns,
    timeout_seconds: f.timeout_seconds,
    enabled: f.enabled,
    // 工作台绑定：Agent 自带的知识库 / 技能 / 插件 / 工作流
    kb_id: formKbId.value || '',
    skills: formSkills.value,
    plugins: formPlugins.value,
    workflows: formWorkflows.value,
  }
  editorSaving.value = true
  try {
    if (editingId.value) {
      await updateAgent(editingId.value, body)
      message.success(t('已保存'))
    } else {
      await createAgent(body)
      message.success(t('已创建'))
    }
    editorOpen.value = false
    await loadAgents()
  } catch (e: any) {
    message.error(t('保存失败: {error}', { error: e?.response?.data?.error || e?.message || '' }))
  } finally {
    editorSaving.value = false
  }
}

// ── 启停 / 删除 ──
async function toggleEnabled(a: Agent) {
  try {
    await updateAgent(a.id, { enabled: !a.enabled })
    a.enabled = !a.enabled
  } catch {
    message.error(t('操作失败'))
  }
}

// ── 共享可见性（团队共享 / 私有；owner-only，非属主 403 提示）──
async function toggleVisibility(a: AgentRow) {
  const next = a.visibility === 'tenant' ? 'private' : 'tenant'
  try {
    await setAgentVisibility(a.id, next)
    message.success(next === 'tenant' ? t('已共享给团队') : t('已设为私有'))
    await loadAgents()
  } catch (e: any) {
    const raw = e?.response?.data
    const msg = raw?.message || raw?.detail || raw?.error || ''
    if (e?.response?.status === 403) {
      message.error(t('只能操作自己创建的 Agent') + (msg ? `：${msg}` : ''))
    } else {
      message.error(t('操作失败') + (msg ? `：${msg}` : ''))
    }
  }
}

function requestDelete(a: Agent) {
  Modal.confirm({
    title: t('删除 Agent'),
    content: t('确定删除「{name}」？其运行记录也会一并删除。', { name: a.name }),
    okText: t('删除'),
    okButtonProps: { danger: true },
    cancelText: t('取消'),
    onOk: async () => {
      try {
        await deleteAgent(a.id)
        message.success(t('已删除'))
        await loadAgents()
        await loadSessions()
      } catch {
        message.error(t('删除失败'))
      }
    },
  })
}

// ── 互联互通：在对话中发起会话（携带 Agent 配置上下文）──
function chatWithAgent(a: Agent) {
  router.push({ path: '/chat', query: { agent: a.id } })
}

// ── 运行 + 轮询 ──
const runOpen = ref(false)
const runTarget = ref<Agent | null>(null)
const runTask = ref('')
const runSession = ref<AgentSession | null>(null)
const runSubmitting = ref(false)
let pollTimer: number | undefined

function stopPolling() {
  if (pollTimer !== undefined) { window.clearInterval(pollTimer); pollTimer = undefined }
}

function startPolling(sessionId: string) {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    try {
      const s = await getAgentSession(sessionId)
      runSession.value = s
      // 同步到历史列表（若有）
      const idx = sessions.value.findIndex(x => x.id === s.id)
      if (idx >= 0) sessions.value[idx] = s
      if (s.status === 'completed' || s.status === 'failed') stopPolling()
    } catch {
      stopPolling()
    }
  }, 2000)
}

function openRun(a: Agent) {
  runTarget.value = a
  runTask.value = ''
  runSession.value = null
  runSubmitting.value = false
  stopPolling()
  runOpen.value = true
}

async function submitRun() {
  if (!runTarget.value) return
  const task = runTask.value.trim()
  if (!task) { message.warning(t('请输入任务')); return }
  runSubmitting.value = true
  try {
    const s = await runAgent(runTarget.value.id, task)
    runSession.value = s
    sessions.value.unshift(s)
    message.success(t('任务已派发，正在执行…'))
    startPolling(s.id)
  } catch (e: any) {
    message.error(t('派发失败: {error}', { error: e?.response?.data?.error || e?.message || '' }))
  } finally {
    runSubmitting.value = false
  }
}

function closeRun() {
  stopPolling()
  runOpen.value = false
}

// ── 历史结果查看 ──
const detailOpen = ref(false)
const detailSession = ref<AgentSession | null>(null)

async function openDetail(s: AgentSession) {
  try {
    const full = await getAgentSession(s.id)
    detailSession.value = full
  } catch {
    detailSession.value = s
  }
  detailOpen.value = true
}

// ── 结果展示 ──
// 解析逻辑移到 utils：同一份逻辑要供「存入知识库」复用，且能被单测覆盖
function parseResult(s: AgentSession): any {
  return parseAgentResult(s.result)
}

/** 把这次运行的结果带进对话继续讨论（内容经 sessionStorage 投递，避免超长 URL） */
function continueInChat() {
  const session = detailSession.value
  if (!session) return
  const parsed = parseResult(session)
  const text = parsed.output || parsed.error || session.result || ''
  if (!text) {
    message.warning(t('这次运行没有可带入对话的内容'))
    return
  }
  setChatPrefill({
    title: t('运行结果 · {name}', { name: session.agent_name || 'Agent' }),
    text: String(text),
    source: 'agent',
  })
  detailOpen.value = false
  router.push('/chat')
}

// ── 运行结果存入知识库 ──
// 复用对话那边的组件：Agent 结果同样沉淀成 Markdown 文档，走同一条分片上传链路
const saveToKbOpen = ref(false)
const saveToKbContent = ref('')
const saveToKbTitle = ref('')

function openSaveToKb(session: AgentSession | null) {
  if (!session) return
  const markdown = agentResultToMarkdown(session)
  if (!markdown) {
    message.warning(t('这次运行没有可沉淀的内容'))
    return
  }
  saveToKbContent.value = markdown
  saveToKbTitle.value = t('运行结果 · {name}', { name: session.agent_name || 'Agent' })
  saveToKbOpen.value = true
}

const statusMeta: Record<string, { label: string; color: string; icon: any }> = {
  pending: { label: t('排队中'), color: 'default', icon: ClockCircleOutlined },
  running: { label: t('执行中'), color: 'processing', icon: SyncOutlined },
  completed: { label: t('已完成'), color: 'success', icon: CheckCircleOutlined },
  failed: { label: t('失败'), color: 'error', icon: CloseCircleOutlined },
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getMonth() + 1}-${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function toolCount(a: Agent): number {
  return Array.isArray(a.tools) ? a.tools.length : 0
}
</script>

<template>
  <div class="agents-page">
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="page-title">
          Agents
        </h1>
        <p class="page-sub">
          {{ $t('创建专属 Agent，定义提示词与工具，一键派发真实执行') }}
        </p>
      </div>
      <Button
        type="primary"
        @click="openCreate"
      >
        <template #icon>
          <PlusOutlined />
        </template>
        {{ $t('新建 Agent') }}
      </Button>
    </div>

    <Tabs
      v-model:active-key="activeTab"
      class="agents-tabs"
    >
      <!-- ── Tab 1：我的 Agent ── -->
      <TabPane
        key="agents"
        :tab="$t('我的 Agent')"
      >
        <PageSkeleton
          v-if="loadingAgents"
          variant="cards"
          :columns="3"
          :rows="6"
          :header="false"
        />
        <EmptyState
          v-else-if="errorAgents"
          size="page"
          :icon="markRaw(RobotOutlined)"
          :description="$t('加载失败')"
          :hint="$t('无法获取 Agent 列表，请稍后重试')"
        >
          <Button
            type="primary"
            @click="loadAgents"
          >
            {{ $t('重试') }}
          </Button>
        </EmptyState>
        <EmptyState
          v-else-if="agents.length === 0"
          size="page"
          :icon="markRaw(RobotOutlined)"
          :description="$t('还没有 Agent')"
          :hint="$t('点击右上角「新建 Agent」，定义提示词与工具，开始派发任务')"
        >
          <Button
            type="primary"
            @click="openCreate"
          >
            <template #icon>
              <PlusOutlined />
            </template>
            {{ $t('新建 Agent') }}
          </Button>
        </EmptyState>
        <div
          v-else
          class="agent-grid"
        >
          <div
            v-for="a in agents"
            :key="a.id"
            class="agent-card"
            :class="{ disabled: !a.enabled }"
          >
            <div class="card-top">
              <span class="card-avatar"><RobotOutlined /></span>
              <div class="card-titles">
                <span class="card-name">{{ a.name }}</span>
                <span class="card-desc">{{ a.description || $t('暂无描述') }}</span>
              </div>
              <Dropdown
                trigger="click"
                placement="bottomRight"
              >
                <Button
                  type="text"
                  size="small"
                  class="card-more"
                  :title="$t('更多操作')"
                  @click.stop
                >
                  <template #icon>
                    <EditOutlined />
                  </template>
                </Button>
                <template #overlay>
                  <Menu class="card-menu">
                    <MenuItem
                      key="edit"
                      @click="openEdit(a)"
                    >
                      <EditOutlined class="menu-icon" />{{ $t('编辑') }}
                    </MenuItem>
                    <MenuItem
                      key="toggle"
                      @click="toggleEnabled(a)"
                    >
                      <template v-if="a.enabled">
                        <StopOutlined class="menu-icon" />{{ $t('停用') }}
                      </template>
                      <template v-else>
                        <PlayCircleOutlined class="menu-icon" />{{ $t('启用') }}
                      </template>
                    </MenuItem>
                    <MenuItem
                      key="visibility"
                      @click="toggleVisibility(a)"
                    >
                      <template v-if="a.visibility === 'tenant'">
                        <LockOutlined class="menu-icon" />{{ $t('设为私有') }}
                      </template>
                      <template v-else>
                        <TeamOutlined class="menu-icon" />{{ $t('共享给团队') }}
                      </template>
                    </MenuItem>
                    <MenuItem
                      key="delete"
                      danger
                      @click="requestDelete(a)"
                    >
                      <DeleteOutlined class="menu-icon" />{{ $t('删除') }}
                    </MenuItem>
                  </Menu>
                </template>
              </Dropdown>
            </div>
            <div class="card-meta">
              <Tag
                v-if="a.visibility === 'tenant'"
                color="green"
              >
                {{ $t('团队共享') }}
              </Tag>
              <Tag :color="a.enabled ? 'green' : 'default'">
                {{ a.enabled ? $t('启用') : $t('已停用') }}
              </Tag>
              <Tag>{{ $t('{n} 工具', { n: toolCount(a) }) }}</Tag>
              <Tag>{{ $t('最多 {n} 轮', { n: a.max_turns }) }}</Tag>
            </div>
            <div class="card-actions">
              <Button
                type="primary"
                size="small"
                :disabled="!a.enabled"
                @click="openRun(a)"
              >
                <template #icon>
                  <PlayCircleOutlined />
                </template>
                {{ $t('运行') }}
              </Button>
              <Button
                size="small"
                :title="$t('在对话中发起会话')"
                @click="chatWithAgent(a)"
              >
                <template #icon>
                  <MessageOutlined />
                </template>
                {{ $t('发起对话') }}
              </Button>
            </div>
          </div>
        </div>
      </TabPane>

      <!-- ── Agent 市场 ── -->
      <TabPane
        key="market"
        :tab="$t('市场')"
      >
        <PageSkeleton
          v-if="marketLoading"
          variant="cards"
          :columns="3"
          :rows="6"
          :header="false"
        />
        <EmptyState
          v-else-if="marketError"
          size="page"
          :icon="markRaw(ShopOutlined)"
          :description="$t('市场加载失败')"
          :hint="$t('无法获取市场内容，请稍后重试')"
        >
          <Button
            type="primary"
            @click="loadMarket"
          >
            {{ $t('重试') }}
          </Button>
        </EmptyState>
        <SkillMarketCard
          v-else
          :items="marketItems"
          type="agent"
          :installing-id="marketInstallingId"
          @install="handleMarketInstall"
        />
      </TabPane>

      <!-- ── Tab 2：运行记录 ── -->
      <TabPane
        key="sessions"
        :tab="$t('运行记录')"
      >
        <PageSkeleton
          v-if="loadingSessions"
          variant="list"
          :rows="6"
          :header="false"
        />
        <EmptyState
          v-else-if="errorSessions"
          size="page"
          :icon="markRaw(HistoryOutlined)"
          :description="$t('加载失败')"
          :hint="$t('无法获取运行记录，请稍后重试')"
        >
          <Button
            type="primary"
            @click="loadSessions"
          >
            {{ $t('重试') }}
          </Button>
        </EmptyState>
        <EmptyState
          v-else-if="sessions.length === 0"
          size="page"
          :icon="markRaw(HistoryOutlined)"
          :description="$t('暂无运行记录')"
          :hint="$t('从「我的 Agent」派发任务后，运行结果将在此显示')"
        />
        <div
          v-else
          class="session-list"
        >
          <div
            v-for="s in sessions"
            :key="s.id"
            class="session-row"
          >
            <span
              class="session-status-icon"
              :class="s.status"
            >
              <component
                :is="statusMeta[s.status]?.icon"
                v-if="statusMeta[s.status]"
              />
            </span>
            <div class="session-main">
              <div class="session-task">
                {{ s.task }}
              </div>
              <div class="session-meta">
                <Tag :color="statusMeta[s.status]?.color">
                  {{ statusMeta[s.status]?.label || s.status }}
                </Tag>
                <span
                  v-if="s.agent_name"
                  class="session-agent"
                >{{ s.agent_name }}</span>
                <span class="session-time">{{ formatTime(s.created_at) }}</span>
              </div>
            </div>
            <Button
              size="small"
              type="text"
              @click="openDetail(s)"
            >
              {{ $t('查看结果') }}
            </Button>
          </div>
        </div>
      </TabPane>
    </Tabs>

    <!-- ── 新建/编辑 Modal ── -->
    <Modal
      :open="editorOpen"
      :title="editingId ? $t('编辑 Agent') : $t('新建 Agent')"
      :confirm-loading="editorSaving"
      width="640px"
      :ok-text="$t('保存')"
      :cancel-text="$t('取消')"
      @ok="saveEditor"
      @cancel="editorOpen = false"
    >
      <div class="editor-form">
        <div class="form-row">
          <label class="form-label">{{ $t('名称 *') }}</label>
          <Input
            v-model:value="form.name"
            :placeholder="$t('如：数据分析师')"
            :maxlength="60"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('描述') }}</label>
          <Input
            v-model:value="form.description"
            :placeholder="$t('一句话说明 Agent 的职责')"
            :maxlength="200"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('系统提示词') }}</label>
          <Input.TextArea
            v-model:value="form.system_prompt"
            :rows="5"
            :placeholder="$t('定义 Agent 的角色、行为准则与目标…')"
          />
        </div>
        <div class="form-row form-grid">
          <div class="form-field">
            <label class="form-label">{{ $t('模型') }}</label>
            <Input
              v-model:value="form.model"
              placeholder="deepseek-chat"
            />
          </div>
          <div class="form-field">
            <label class="form-label">{{ $t('最大轮次') }}</label>
            <InputNumber
              v-model:value="form.max_turns"
              :min="1"
              :max="30"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label class="form-label">{{ $t('超时（秒）') }}</label>
            <InputNumber
              v-model:value="form.timeout_seconds"
              :min="10"
              :max="3600"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label class="form-label">Temperature</label>
            <InputNumber
              v-model:value="form.temperature"
              :min="0"
              :max="2"
              :step="0.1"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label class="form-label">Max Tokens</label>
            <InputNumber
              v-model:value="form.max_tokens"
              :min="256"
              :max="32768"
              :step="512"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label class="form-label">{{ $t('启用') }}</label>
            <Switch v-model:checked="form.enabled" />
          </div>
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('工具（JSON）') }}</label>
          <Input.TextArea
            v-model:value="form.tools_text"
            :rows="4"
            :placeholder="$t('{jsonExample}', { jsonExample: '[{&quot;name&quot;:&quot;shell_exec&quot;,&quot;description&quot;:&quot;执行命令&quot;,&quot;parameters&quot;:{&quot;type&quot;:&quot;object&quot;,&quot;properties&quot;:{}}}]' })"
            class="tools-input"
          />
          <ToolPicker v-model="form.tools_text" />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('默认知识库') }}</label>
          <Select
            v-model:value="formKbId"
            :options="kbOptions.map(b => ({ value: b.id, label: b.name }))"
            :loading="bindingLoading"
            :placeholder="$t('派发时用它做检索（可留空）')"
            allow-clear
            show-search
            option-filter-prop="label"
            class="binding-select"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('技能') }}</label>
          <Select
            v-model:value="formSkills"
            mode="multiple"
            :options="skillOptions.map(s => ({ value: s.name, label: s.name }))"
            :loading="bindingLoading"
            :placeholder="$t('只启用选中的技能（留空 = 全部已安装）')"
            allow-clear
            show-search
            option-filter-prop="label"
            class="binding-select"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('插件') }}</label>
          <Select
            v-model:value="formPlugins"
            mode="multiple"
            :options="pluginOptions.map(p => ({ value: p.name, label: p.name }))"
            :loading="bindingLoading"
            :placeholder="$t('只放这些插件提供的工具（留空 = 不筛选）')"
            allow-clear
            show-search
            option-filter-prop="label"
            class="binding-select"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('工作流') }}</label>
          <!-- value 用 id 而非 name：agents.workflows 存的是工作流 id（引擎按 id 载图） -->
          <Select
            v-model:value="formWorkflows"
            mode="multiple"
            :options="workflowOptions.map(w => ({ value: w.id, label: w.name }))"
            :loading="bindingLoading"
            :placeholder="$t('派发时按顺序执行（留空 = 不绑定）')"
            allow-clear
            show-search
            option-filter-prop="label"
            class="binding-select"
          />
        </div>
      </div>
    </Modal>

    <!-- ── 运行 Modal ── -->
    <Modal
      :open="runOpen"
      :title="$t('运行「{name}」', { name: runTarget?.name || '' })"
      :footer="null"
      :closable="true"
      width="600px"
      @cancel="closeRun"
    >
      <Input.TextArea
        v-model:value="runTask"
        :rows="3"
        :placeholder="$t('描述要完成的任务…')"
        :disabled="!!runSession && runSession.status === 'running'"
      />
      <div class="run-actions">
        <Button
          type="primary"
          :loading="runSubmitting"
          :disabled="!!runSession && (runSession.status === 'running' || runSession.status === 'pending')"
          @click="submitRun"
        >
          <template #icon>
            <PlayCircleOutlined />
          </template>
          {{ $t('派发任务') }}
        </Button>
      </div>

      <div
        v-if="runSession"
        class="run-result"
      >
        <Alert
          v-if="runSession.status === 'running' || runSession.status === 'pending'"
          type="info"
          show-icon
          :message="runSession.status === 'pending' ? $t('任务排队中…') : $t('Agent 正在执行…（LLM 推理 + 工具调用）')"
        />
        <Alert
          v-else-if="runSession.status === 'failed'"
          type="error"
          show-icon
          :message="$t('执行失败')"
          :description="parseResult(runSession).error || parseResult(runSession).output || $t('未知错误')"
        />
        <template v-else-if="runSession.status === 'completed'">
          <Alert
            type="success"
            show-icon
            :message="$t('执行完成')"
          />
          <div class="result-block">
            <div class="result-label">
              {{ $t('输出') }}
            </div>
            <pre class="result-output">{{ parseResult(runSession).output || $t('（无输出）') }}</pre>
          </div>
          <div
            v-if="parseResult(runSession).token_usage || parseResult(runSession).duration || parseResult(runSession).tool_calls?.length"
            class="result-meta"
          >
            <Tag v-if="parseResult(runSession).token_usage">
              tokens: {{ JSON.stringify(parseResult(runSession).token_usage) }}
            </Tag>
            <Tag v-if="parseResult(runSession).duration">
              {{ $t('耗时 {n}s', { n: parseResult(runSession).duration.toFixed(1) }) }}
            </Tag>
            <Tag v-if="parseResult(runSession).tool_calls?.length">
              {{ $t('工具调用 {n} 次', { n: parseResult(runSession).tool_calls.length }) }}
            </Tag>
          </div>
          <div class="result-actions">
            <Button
              ghost
              @click="openSaveToKb(runSession)"
            >
              {{ $t('存入知识库') }}
            </Button>
          </div>
        </template>
      </div>
    </Modal>

    <!-- ── 历史结果详情 Modal ── -->
    <Modal
      :open="detailOpen"
      :title="$t('运行结果 · {name}', { name: detailSession?.agent_name || '' })"
      :footer="null"
      width="640px"
      @cancel="detailOpen = false"
    >
      <div
        v-if="detailSession"
        class="run-result"
      >
        <div class="session-meta">
          <Tag :color="statusMeta[detailSession.status]?.color">
            {{ statusMeta[detailSession.status]?.label || detailSession.status }}
          </Tag>
          <span class="session-time">{{ formatTime(detailSession.created_at) }}</span>
        </div>
        <Alert
          v-if="detailSession.status === 'failed'"
          type="error"
          show-icon
          :message="$t('执行失败')"
          :description="parseResult(detailSession).error || parseResult(detailSession).output || $t('未知错误')"
        />
        <div class="result-block">
          <div class="result-label">
            {{ $t('任务') }}
          </div>
          <pre class="result-output">{{ detailSession.task }}</pre>
        </div>
        <div
          v-if="parseResult(detailSession).output"
          class="result-block"
        >
          <div class="result-label">
            {{ $t('输出') }}
          </div>
          <pre class="result-output">{{ parseResult(detailSession).output }}</pre>
        </div>
        <div
          v-if="parseResult(detailSession).token_usage || parseResult(detailSession).duration || parseResult(detailSession).tool_calls?.length"
          class="result-meta"
        >
          <Tag v-if="parseResult(detailSession).token_usage">
            tokens: {{ JSON.stringify(parseResult(detailSession).token_usage) }}
          </Tag>
          <Tag v-if="parseResult(detailSession).duration">
            {{ $t('耗时 {n}s', { n: parseResult(detailSession).duration.toFixed(1) }) }}
          </Tag>
          <Tag v-if="parseResult(detailSession).tool_calls?.length">
            {{ $t('工具调用 {n} 次', { n: parseResult(detailSession).tool_calls.length }) }}
          </Tag>
        </div>
        <div class="result-actions">
          <Button
            ghost
            @click="openSaveToKb(detailSession)"
          >
            {{ $t('存入知识库') }}
          </Button>
          <Button
            type="primary"
            ghost
            @click="continueInChat"
          >
            {{ $t('在对话中继续') }}
          </Button>
        </div>
      </div>
    </Modal>

    <!-- ── 存入知识库（复用对话侧的组件与分片上传链路）── -->
    <SaveToKnowledgeDialog
      v-model:open="saveToKbOpen"
      :content="saveToKbContent"
      :default-title="saveToKbTitle"
    />
  </div>
</template>

<style scoped>
.agents-page { max-width: 1080px; margin: 0 auto; padding: 28px 24px 60px; }
.result-actions { margin-top: 16px; display: flex; justify-content: flex-end; gap: 8px; }
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.page-title { font-size: 24px; font-weight: 700; margin: 0; letter-spacing: -0.01em; }
.page-sub { margin: 4px 0 0; color: var(--text-tertiary); font-size: 13px; }
.agents-tabs :deep(.ant-tabs-nav) { margin-bottom: 20px; }

/* ── Agent 卡片 ── */
.agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.agent-card {
  display: flex; flex-direction: column; gap: 12px;
  padding: 18px 18px 14px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  transition: transform var(--dur-normal) ease, border-color var(--dur-normal) ease, box-shadow var(--dur-normal) ease;
}
.agent-card:hover { transform: translateY(-2px); border-color: var(--primary); box-shadow: var(--shadow-lg); }
.agent-card.disabled { opacity: 0.6; }
.card-top { display: flex; align-items: flex-start; gap: 10px; }
.card-avatar {
  flex: none; width: 38px; height: 38px; border-radius: 10px;
  background: var(--primary-bg); color: var(--primary);
  display: inline-flex; align-items: center; justify-content: center; font-size: 18px;
}
.card-titles { flex: 1; min-width: 0; }
.card-name { display: block; font-size: 15px; font-weight: 600; color: var(--text-primary); }
.card-desc {
  display: block; margin-top: 3px; font-size: 12px; color: var(--text-tertiary);
  line-height: 1.5; overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}
.card-more { color: var(--text-tertiary); }
.card-meta { display: flex; flex-wrap: wrap; gap: 4px; }
.card-actions { display: flex; justify-content: flex-end; }
.card-menu { min-width: 140px; border-radius: 10px; padding: 4px; box-shadow: var(--shadow-lg); }
.card-menu :deep(.ant-dropdown-menu-item) { display: flex; align-items: center; gap: 8px; font-size: 13px; border-radius: 6px; }
.menu-icon { font-size: 14px; }

/* ── 运行记录 ── */
.session-list { display: flex; flex-direction: column; gap: 8px; }
.session-row {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
}
.session-status-icon { flex: none; width: 30px; height: 30px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 14px; }
.session-status-icon.pending { background: var(--bg-secondary); color: var(--text-tertiary); }
.session-status-icon.running { background: var(--primary-bg); color: var(--primary); }
.session-status-icon.completed { background: var(--success-bg); color: var(--success); }
.session-status-icon.failed { background: var(--error-bg); color: var(--error); }
.session-main { flex: 1; min-width: 0; }
.session-task { font-size: 13px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.session-meta { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.session-agent { font-size: 12px; color: var(--text-secondary); }
.session-time { font-size: 12px; color: var(--text-tertiary); }

/* ── 表单 ── */
.editor-form { display: flex; flex-direction: column; gap: 14px; }
.form-row { display: flex; flex-direction: column; gap: 6px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.form-field { display: flex; flex-direction: column; gap: 6px; }
.form-label { font-size: 12px; color: var(--text-secondary); font-weight: 500; }
.w-full { width: 100%; }
.binding-select { width: 100%; }
.tools-input :deep(textarea) { font-family: var(--font-mono); font-size: 12px; }

/* ── 运行结果 ── */
.run-actions { display: flex; justify-content: flex-end; margin-top: 12px; }
.run-result { margin-top: 16px; display: flex; flex-direction: column; gap: 10px; }
.result-block { border: 1px solid var(--border-card); border-radius: 10px; background: var(--bg-secondary); overflow: hidden; }
.result-label { padding: 8px 12px; font-size: 12px; color: var(--text-tertiary); border-bottom: 1px solid var(--border-card); }
.result-output { margin: 0; padding: 12px; font-family: var(--font-mono); font-size: 12.5px; line-height: 1.7; color: var(--text-primary); white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow-y: auto; }
.result-meta { display: flex; flex-wrap: wrap; gap: 4px; }

@media (max-width: 640px) {
  .agents-page { padding: 20px 16px 48px; }
  .form-grid { grid-template-columns: 1fr; }
}

@media (max-width: 768px) {
  .page-head { flex-direction: column; align-items: flex-start; }
  .page-head > .ant-btn { width: 100%; }
  .session-row { flex-wrap: wrap; row-gap: 8px; }
  .session-main { flex-basis: calc(100% - 42px); }
  .session-task { white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
}
</style>
