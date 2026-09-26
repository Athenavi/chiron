<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { VueFlow, useVueFlow, Handle, Position } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import {
  Button, Input, Select, Drawer, Form, FormItem,
  Empty, Popconfirm, Tag, InputNumber, message, Modal,
} from 'ant-design-vue'
import {
  SaveOutlined, PlayCircleOutlined, DeleteOutlined,
  CloseOutlined, UnorderedListOutlined, CopyOutlined,
  AlignCenterOutlined, HistoryOutlined, MessageOutlined,
  RocketOutlined,
} from '@ant-design/icons-vue'
import { api, listAgents, listTemplates, useTemplate } from '../api'
import type { Agent, TemplateItem } from '../api'
import { useAuthStore } from '../stores/auth'
import { collectBindingUsage } from '../utils/kbUsage'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import AttachToAgentDialog from '../components/common/AttachToAgentDialog.vue'
import type { Node, Edge, Connection } from '@vue-flow/core'
import { setChatPrefill } from '../components/chat/chatPrefill'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const authStore = useAuthStore()
const router = useRouter()

 // S 安全：user_id 从 authStore.user 读取（token 已迁至 httpOnly cookie，JS 不可读）
function getUserIdFromToken(): string | null {
  return authStore.user?.id || null
}

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  return isNaN(d.getTime()) ? '' : d.toLocaleString()
}

/** 把这条执行记录的全部节点输出带进对话继续讨论（内容经 sessionStorage 投递） */
function continueInChat(inst: InstanceRecord) {
  const parts: string[] = []
  const results = (inst.results || {}) as Record<string, { output?: unknown }>
  for (const [nodeId, node] of Object.entries(results)) {
    const out = node?.output
    const text = typeof out === 'string' ? out : out == null ? '' : JSON.stringify(out)
    if (text) parts.push(`【${nodeId}】\n${text}`)
  }
  if (inst.error) parts.push(t('【错误】\n{error}', { error: inst.error }))
  if (parts.length === 0) {
    message.warning(t('chat.this_execution_record_has_no_content_to_bring_into_the_conversation'))
    return
  }
  setChatPrefill({
    title: t('工作流执行结果 · {name}', { name: inst.workflow_name || t('workflow.untitled_workflow') }),
    text: parts.join('\n\n'),
    source: 'workflow',
  })
  showHistory.value = false
  router.push('/chat')
}

// ── Types ──
interface GraphNodeBackend {
  id: string
  label: string
  node_type: string
  config?: Record<string, any>
}

interface GraphEdgeBackend {
  source_id: string
  target_id: string
  condition?: string
  label?: string
}

interface GraphRecord {
  id: string
  name: string
  graph_json: string
  created_at: string
  updated_at: string
}

interface InstanceRecord {
  id: string
  workflow_id: string
  workflow_name: string
  status: string
  results: Record<string, { status: string; output: any }>
  error?: string
  created_at: string
  updated_at: string
}

// ── Node type definitions ──
const nodeTypes = [
  { type: 'input', label: t('common.input'), color: '#22c55e', icon: '📥', description: t('admin.receive_user_input') },
  { type: 'llm', label: 'LLM', color: '#8b5cf6', icon: '🧠', description: t('agent.call_the_llm') },
  { type: 'tool', label: t('agent.tools'), color: '#3b82f6', icon: '🔧', description: t('auth.execute_registration_tool') },
  { type: 'skill', label: t('agent.skill'), color: '#ec4899', icon: '🎯', description: t('agent.call_an_installed_skill') },
  { type: 'knowledge', label: t('knowledge.knowledge_base'), color: '#14b8a6', icon: '📚', description: t('knowledge.retrieve_knowledge_base_snippets') },
  { type: 'agent', label: 'Agent', color: '#6366f1', icon: '🤖', description: t('workflow.call_an_installed_agent_to_run_a_subtask') },
  { type: 'condition', label: t('common.condition'), color: '#f59e0b', icon: '🔀', description: t('common.conditional_branch') },
  { type: 'output', label: t('common.output_2'), color: '#6b7280', icon: '📤', description: t('common.output_result') },
]

const toolOptions = [
  { value: 'browser_navigate', label: t('common.browser_navigation') },
  { value: 'browser_click', label: t('common.click_element') },
  { value: 'browser_type', label: t('common.enter_text') },
  { value: 'browser_read', label: t('common.read_page') },
  { value: 'browser_screenshot', label: t('common.screenshot') },
  { value: 'browser_scroll', label: t('common.scroll_page') },
  { value: 'browser_get_state', label: t('common.get_page_status') },
  { value: 'browser_tab_list', label: t('common.list_tabs') },
  { value: 'browser_tab_create', label: t('common.new_tab') },
  { value: 'browser_tab_switch', label: t('common.switch_tab') },
  { value: 'browser_tab_close', label: t('common.close_tab') },
  { value: 'web_search', label: t('common.web_search') },
  { value: 'shell_exec', label: t('common.execute_command') },
]

const modelOptions = [
  { value: 'deepseek-chat', label: 'DeepSeek Chat' },
  { value: 'deepseek-reasoner', label: 'DeepSeek Reasoner' },
  { value: 'gpt-4o-mini', label: 'GPT-4o mini' },
  { value: 'gpt-4o', label: 'GPT-4o' },
  { value: 'claude-sonnet-4-20250514', label: 'Claude Sonnet 4' },
]

// ── Vue Flow ──
const { findNode, addNodes, addEdges, removeNodes, getNodes, getEdges, getSelectedNodes, fitView } = useVueFlow({
  defaultEdgeOptions: { type: 'smoothstep', animated: true },
  multiSelectionKeyCode: ['Shift', 'Meta', 'Control'],
})

// ── State ──
const nodes = ref<Node[]>([])
const edges = ref<Edge[]>([])
const workflowName = ref(t('workflow.new_workflow'))
const workflowId = ref<string | null>(null)
const savedWorkflows = ref<GraphRecord[]>([])
const instances = ref<InstanceRecord[]>([])
const showPanel = ref(false)
const selectedNode = ref<Node | null>(null)
const isExecuting = ref(false)
const executionLogs = ref<string[]>([])
const executionResults = ref<Record<string, { status: string; output: any }>>({})
const showDrawer = ref(false)
const showHistory = ref(false)
const dragNodeType = ref<string | null>(null)

// ── Node config form fields ──
const editLabel = ref('')
const editSystemPrompt = ref('')
const editPrompt = ref('')
const editUserMessage = ref('')
const editToolName = ref('')
const editModel = ref('deepseek-chat')
const editRetries = ref(0)
const editCondition = ref('')
const editVariable = ref('')
const editSkillName = ref('')
const editSkillParams = ref('')
const editKbId = ref('')
const editKbQuery = ref('')
const editKbTopK = ref(5)

// ── Agent 节点配置字段 ──
const editAgentId = ref('')
const editAgentName = ref('')
const editAgentTask = ref('')
const editMaxTurns = ref(5)

// ── Agent 列表（复用 /v1/agents API） ──
const agentList = ref<Agent[]>([])
const agentLoading = ref(false)
const agentLoadFailed = ref(false)
const agentOptions = computed(() =>
  agentList.value.map(a => ({ value: a.id, label: a.name }))
)

/**
 * 该工作流被哪些 Agent 装配（反向引用）。
 *
 * 与知识库页 / 技能页同族（utils/kbUsage.ts 的 collectBindingUsage）：后端没有
 * "谁在用我"的查询，而 Agent 列表前端本来就会拉，自己算一遍比新增一次后端往返划算。
 */
const workflowAgentUsage = computed(() =>
  collectBindingUsage(agentList.value as readonly Record<string, unknown>[], 'workflows')
)

function agentsUsingWorkflow(id: string): string[] {
  return workflowAgentUsage.value.get(id)?.agents ?? []
}

// 取不到 Agent 列表时回退手动输入
const agentManualMode = computed(() =>
  agentLoadFailed.value || (!agentLoading.value && agentList.value.length === 0)
)
// 模型下拉：默认模型列表 + 当前值（Agent 自带模型可能不在预设列表中）
const agentModelOptions = computed(() => {
  const list = [...modelOptions]
  if (editModel.value && !list.some(o => o.value === editModel.value)) {
    list.push({ value: editModel.value, label: editModel.value })
  }
  return list
})

// ── Helper ──
let nodeCounter = 0

function genNodeId(type: string): string {
  nodeCounter++
  return `${type}_${nodeCounter}`
}

function getNodeColor(type: string): string {
  return nodeTypes.find(n => n.type === type)?.color || '#6b7280'
}

function getNodeIcon(type: string): string {
  return nodeTypes.find(n => n.type === type)?.icon || '📦'
}

// ── Drag & Drop ──
function onDragStart(event: DragEvent, type: string) {
  if (event.dataTransfer) {
    event.dataTransfer.setData('application/vueflow', type)
    event.dataTransfer.effectAllowed = 'move'
  }
  dragNodeType.value = type
}

function onDragOver(event: DragEvent) {
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
}

function onDrop(event: DragEvent) {
  const type = event.dataTransfer?.getData('application/vueflow')
  if (!type) return
  const flowEl = document.querySelector('.vue-flow')
  if (!flowEl) return
  const rect = flowEl.getBoundingClientRect()
  const position = { x: event.clientX - rect.left - 80, y: event.clientY - rect.top - 30 }
  const nodeDef = nodeTypes.find(n => n.type === type)
  addNodes([{
    id: genNodeId(type),
    type,
    position,
    data: { label: nodeDef?.label || type, nodeType: type, color: nodeDef?.color || '#6b7280', icon: nodeDef?.icon || '📦' },
  }])
}

// ── Node selection ──
function onNodeClick(_event: any) {
  const node = _event.node
  selectedNode.value = node
  showPanel.value = true
  editLabel.value = node.data?.label || ''
  const cfg = node.data?.config || {}
  editSystemPrompt.value = cfg.system_prompt || ''
  editPrompt.value = cfg.prompt || ''
  editUserMessage.value = cfg.user_message || ''
  editToolName.value = cfg.tool_name || ''
  editModel.value = cfg.model || 'deepseek-chat'
  editRetries.value = cfg.retries || 0
  editCondition.value = cfg.condition || ''
  editVariable.value = cfg.variable || '$'
  editSkillName.value = cfg.skill_name || ''
  editSkillParams.value = cfg.params ? JSON.stringify(cfg.params) : ''
  editKbId.value = cfg.kb_id || ''
  editKbQuery.value = cfg.query || ''
  editKbTopK.value = cfg.top_k || 5
  editAgentId.value = cfg.agent_id || ''
  editAgentName.value = cfg.name || ''
  editAgentTask.value = cfg.task || ''
  editMaxTurns.value = cfg.max_turns || 5
}

function onPaneClick() {
  selectedNode.value = null
  showPanel.value = false
}

function applyNodeConfig() {
  if (!selectedNode.value) return
  const node = findNode(selectedNode.value.id)
  if (!node) return
  node.data = {
    ...node.data,
    label: editLabel.value,
    config: {
      system_prompt: editSystemPrompt.value || undefined,
      prompt: editPrompt.value || undefined,
      user_message: editUserMessage.value || undefined,
      tool_name: editToolName.value || undefined,
      model: editModel.value || undefined,
      retries: editRetries.value > 0 ? editRetries.value : undefined,
      condition: editCondition.value || undefined,
      variable: editVariable.value || undefined,
      skill_name: editSkillName.value || undefined,
      params: parseJSON(editSkillParams.value),
      kb_id: editKbId.value || undefined,
      query: editKbQuery.value || undefined,
      top_k: editKbTopK.value > 0 ? editKbTopK.value : undefined,
      name: editAgentName.value || undefined,
      max_turns: editMaxTurns.value > 0 ? editMaxTurns.value : undefined,
      task: editAgentTask.value || undefined,
      agent_id: editAgentId.value || undefined,
    },
  }
}

function parseJSON(s: string): any {
  if (!s) return undefined
  try { return JSON.parse(s) } catch { return { input: s } }
}

// ── Agent 列表 ──
async function loadAgentList() {
  agentLoading.value = true
  agentLoadFailed.value = false
  try {
    agentList.value = await listAgents()
  } catch {
    agentList.value = []
    agentLoadFailed.value = true
  } finally {
    agentLoading.value = false
  }
}

// 选中 Agent 后自动填入 name/system_prompt/model/max_turns（可编辑覆盖）
function onAgentSelect(value: unknown) {
  if (!value) {
    editAgentId.value = ''
    return
  }
  const agent = agentList.value.find(a => a.id === value)
  if (!agent) return
  editAgentId.value = agent.id
  editAgentName.value = agent.name || ''
  editSystemPrompt.value = agent.system_prompt || ''
  editModel.value = String(agent.llm_config?.model || editModel.value)
  editMaxTurns.value = agent.max_turns || 5
}

watch([editLabel, editSystemPrompt, editPrompt, editUserMessage, editToolName, editModel, editRetries, editCondition, editVariable, editSkillName, editSkillParams, editKbId, editKbQuery, editKbTopK, editAgentId, editAgentName, editAgentTask, editMaxTurns], () => {
  applyNodeConfig()
})

// ── Delete / duplicate ──
function deleteSelectedNode() {
  if (!selectedNode.value) return
  removeNodes([selectedNode.value.id])
  selectedNode.value = null
  showPanel.value = false
}

function deleteSelectedNodes() {
  const selected = getSelectedNodes.value
  if (selected.length > 0) {
    removeNodes(selected.map(n => n.id))
    selectedNode.value = null
    showPanel.value = false
  }
}

function duplicateSelectedNode() {
  if (!selectedNode.value) return
  const src = findNode(selectedNode.value.id)
  if (!src) return
  const copy: Node = {
    id: genNodeId(src.data?.nodeType || src.type || 'node'),
    type: src.type,
    position: { x: src.position.x + 40, y: src.position.y + 40 },
    data: JSON.parse(JSON.stringify(src.data || {})),
  }
  addNodes([copy])
  message.success(t('workflow.node_copied'))
}

// 快捷键：Delete 删除选中；Ctrl+D 复制
function onKeydown(e: KeyboardEvent) {
  const tag = (e.target as HTMLElement)?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
  if ((e.key === 'Delete' || e.key === 'Backspace')) {
    const selected = getSelectedNodes.value
    if (selected.length > 0) { e.preventDefault(); deleteSelectedNodes() }
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'd') {
    e.preventDefault()
    if (selectedNode.value) duplicateSelectedNode()
  }
}

// ── Auto layout（BFS 按层排列） ──
function autoLayout() {
  const allNodes = getNodes.value
  const allEdges = getEdges.value
  if (allNodes.length === 0) return
  const indegree: Record<string, number> = {}
  const adj: Record<string, string[]> = {}
  for (const n of allNodes) indegree[n.id] = 0
  for (const e of allEdges) {
    indegree[e.target] = (indegree[e.target] || 0) + 1
    ;(adj[e.source] = adj[e.source] || []).push(e.target)
  }
  const layer: Record<string, number> = {}
  const queue: string[] = []
  for (const n of allNodes) if (indegree[n.id] === 0) { layer[n.id] = 0; queue.push(n.id) }
  while (queue.length > 0) {
    const id = queue.shift()!
    for (const t of adj[id] || []) {
      layer[t] = Math.max(layer[t] || 0, (layer[id] || 0) + 1)
      if (--indegree[t] === 0) queue.push(t)
    }
  }
  const colCount: Record<number, number> = {}
  for (const n of allNodes) {
    const l = layer[n.id] || 0
    const col = colCount[l] || 0
    colCount[l] = col + 1
    n.position = { x: 40 + l * 220, y: 40 + col * 110 }
  }
  message.success(t('common.auto_layout_applied'))
}

// ── Edge connection（条件节点 handle → 边 label） ──
function onConnect(params: Connection) {
  const handle = params.sourceHandle === 'false' ? 'false' : params.sourceHandle === 'true' ? 'true' : undefined
  addEdges([{
    id: `e-${params.source}-${params.target}-${Date.now()}`,
    source: params.source as string,
    target: params.target as string,
    sourceHandle: params.sourceHandle || undefined,
    targetHandle: params.targetHandle || undefined,
    label: handle,
    data: { condition: handle },
    type: 'smoothstep',
    animated: true,
  }])
}

// ── Convert between VueFlow ↔ Backend format ──
function toBackendFormat(): { nodes: GraphNodeBackend[]; edges: GraphEdgeBackend[]; entry_point: string } {
  const allNodes = getNodes.value
  const allEdges = getEdges.value
  const backendNodes: GraphNodeBackend[] = allNodes.map((n) => {
    const config: Record<string, any> = { ...(n.data?.config || {}) }
    config.position = { x: Math.round(n.position.x), y: Math.round(n.position.y) }
    return { id: n.id, label: n.data?.label || n.id, node_type: n.data?.nodeType || n.type || 'tool', config }
  })
  const backendEdges: GraphEdgeBackend[] = allEdges.map((e) => ({
    source_id: e.source,
    target_id: e.target,
    condition: e.data?.condition || '',
    label: typeof e.label === 'string' ? e.label : '',
  }))
  const inputNode = allNodes.find(n => n.data?.nodeType === 'input')
  return { nodes: backendNodes, edges: backendEdges, entry_point: inputNode?.id || allNodes[0]?.id || '' }
}

function fromBackendFormat(data: any) {
  if (!data) return
  const graphDef = typeof data === 'string' ? JSON.parse(data) : data
  nodes.value = (graphDef.nodes || []).map((n: GraphNodeBackend) => {
    const cfg = n.config || {}
    const pos = cfg.position || { x: 0, y: 0 }
    return {
      id: n.id,
      type: n.node_type,
      position: { x: pos.x || 0, y: pos.y || 0 },
      data: { label: n.label, nodeType: n.node_type, color: getNodeColor(n.node_type), icon: getNodeIcon(n.node_type), config: cfg },
    }
  })
  edges.value = (graphDef.edges || []).map((e: GraphEdgeBackend, i: number) => ({
    id: `e-${e.source_id}-${e.target_id}-${i}`,
    source: e.source_id,
    target: e.target_id,
    label: e.label || undefined,
    data: { condition: e.condition || e.label || undefined },
    type: 'smoothstep',
    animated: true,
  }))
  workflowName.value = graphDef.name || t('workflow.untitled_workflow')
  nodeCounter = nodes.value.length + 10
}

// ── API: Save ──
async function saveWorkflow() {
  const graphData = toBackendFormat()
  const payload: Record<string, any> = {
    id: workflowId.value || undefined,
    name: workflowName.value,
    graph_json: JSON.stringify({ name: workflowName.value, ...graphData }),
    user_id: getUserIdFromToken() || undefined,
  }
  try {
    const resp = await api.post('/v1/graphs', payload)
    workflowId.value = resp.data?.data?.id || resp.data?.id
    message.success(t('workflow.workflow_saved'))
    await loadWorkflows()
  } catch (err: any) {
    message.error(t('保存失败: {error}', { error: err.response?.data?.error || err.message }))
  }
}

// ── API: Load list ──
async function loadWorkflows() {
  try {
    const resp = await api.get('/v1/graphs')
    savedWorkflows.value = (resp.data?.data || []).filter((r: any) => r.id)
  } catch {
    savedWorkflows.value = []
  }
}

function loadWorkflow(record: GraphRecord) {
  workflowId.value = record.id
  workflowName.value = record.name
  fromBackendFormat(record.graph_json)
  executionResults.value = {}
  executionLogs.value = []
  message.success(t('已加载: {name}', { name: record.name }))
}

// ── API: Delete ──
async function deleteWorkflow(id: string) {
  try {
    await api.delete(`/v1/graphs/${id}`)
    message.success(t('common.deleted'))
    await loadWorkflows()
    if (workflowId.value === id) {
      workflowId.value = null
      resetCanvas()
    }
  } catch (err: any) {
    message.error(t('删除失败: {error}', { error: err.response?.data?.error || err.message }))
  }
}

// ── API: Execute（提交后轮询状态） ──
let statusTimer: number | undefined
const loggedNodes = new Set<string>()

function stopStatusPolling() {
  if (statusTimer !== undefined) { window.clearInterval(statusTimer); statusTimer = undefined }
}

/** 运行输入：图里有 input 节点时才需要用户填（见 needsRunInput 的说明） */
const runInputOpen = ref(false)
const runInputValue = ref('')

/** 装配到 Agent：把当前工作流持久绑定到某个 Agent（写回 agents.workflows） */
const attachToAgentOpen = ref(false)

/**
 * 图里是否存在 input 节点 —— 只有这时才需要向用户收集运行输入。
 *
 * 后端 `/v1/graphs/{id}/execute` 一直支持 initial_state，但前端此前恒定提交 `{}`：
 * engine.py 的 `_input_node`（`state[node_id]` → `state["input"]` → 兜底
 * `"[input] {label}"`）于是永远走兜底分支，input 下游的一切 —— knowledge 节点的
 * query（留空时取上游输出）、llm 节点的 user_message（留空时取上游输出）—— 拿到的
 * 都是占位串。任何"依赖运行时输入"的工作流设计因此无法生效，所以这里把输入接上。
 */
const needsRunInput = computed(() =>
  getNodes.value.some(n => (n.data?.nodeType || n.type) === 'input'),
)

function executeWorkflow() {
  if (!workflowId.value) { message.warning(t('workflow.please_save_the_workflow_first')); return }
  if (!needsRunInput.value) {
    void submitWorkflowRun('')
    return
  }
  runInputValue.value = ''
  runInputOpen.value = true
}

function confirmRunInput() {
  const value = runInputValue.value
  runInputOpen.value = false
  void submitWorkflowRun(value)
}

async function submitWorkflowRun(input: string) {
  isExecuting.value = true
  executionLogs.value = [t('common.submitting')]
  executionResults.value = {}
  loggedNodes.clear()
  for (const n of getNodes.value) n.data = { ...n.data, execStatus: 'idle' }
  try {
    // 有输入才放进 initial_state：留空时保持旧行为（各节点走自身配置/兜底）
    const initial_state = input.trim() ? { input } : {}
    const resp = await api.post(`/v1/graphs/${workflowId.value}/execute`, { initial_state })
    const instanceId = resp.data?.data?.instance_id || resp.data?.instance_id
    if (!instanceId) throw new Error(t('common.no_instance_id'))
    message.info(t('workflow.workflow_submitted_running'))
    startStatusPolling(instanceId)
  } catch (err: any) {
    isExecuting.value = false
    executionLogs.value.push(t('❌ 提交失败: {error}', { error: err.response?.data?.error || err.message }))
  }
}

function startStatusPolling(instanceId: string) {
  stopStatusPolling()
  statusTimer = window.setInterval(async () => {
    try {
      const resp = await api.get(`/v1/workflows/${instanceId}/status`)
      const data = resp.data?.data || resp.data
      applyExecutionStatus(data)
      if (data.status === 'completed') {
        executionLogs.value.push(t('common.done'))
        isExecuting.value = false
        stopStatusPolling()
        await loadInstances()
      } else if (data.status === 'error') {
        executionLogs.value.push(t('❌ 执行失败: {error}', { error: data.error || '' }))
        isExecuting.value = false
        stopStatusPolling()
        await loadInstances()
      }
    } catch {
      stopStatusPolling()
      isExecuting.value = false
      executionLogs.value.push(t('errors.status_query_failed'))
    }
  }, 2000)
}

function applyExecutionStatus(data: any) {
  const results = data.results || {}
  executionResults.value = results
  for (const n of getNodes.value) {
    const r = results[n.id]
    n.data = { ...n.data, execStatus: r ? (r.status === 'completed' ? 'completed' : 'error') : 'idle' }
  }
  for (const [nid, r] of Object.entries(results) as [string, any][]) {
    if (loggedNodes.has(nid)) continue
    loggedNodes.add(nid)
    const n = getNodes.value.find(x => x.id === nid)
    const label = n?.data?.label || nid
    if (r.status === 'completed') executionLogs.value.push(`✅ ${label}`)
    else if (r.status === 'error') executionLogs.value.push(`❌ ${label}`)
  }
}

// ── API: 执行历史 ──
async function loadInstances() {
  try {
    const resp = await api.get('/v1/workflows/instances')
    instances.value = resp.data?.data || []
  } catch {
    instances.value = []
  }
}

// ── Reset ──
function resetCanvas() {
  nodes.value = []
  edges.value = []
  workflowName.value = t('workflow.new_workflow')
  workflowId.value = null
  selectedNode.value = null
  showPanel.value = false
  executionResults.value = {}
  executionLogs.value = []
  nodeCounter = 0
}

// ── 互联互通：运行到对话（有 id 传 id，未保存则传画布名称，由后端兼容）──
function runInChat() {
  const value = workflowId.value || workflowName.value || t('workflow.untitled_workflow')
  router.push({ path: '/chat', query: { workflow: value, mode: 'workflow' } })
}

// ── 模板市场（一键使用：加载进画布，不落库）──
// 此前这一整块是**未接线的实现**：数据加载在 onMounted 里每页跑一次、结果丢弃，
// `useWorkflowTemplate` 从不被调用（见路线图 L3-6）。现在由工具栏的「模板」入口驱动。
const templateOpen = ref(false)
const templates = ref<TemplateItem[]>([])
const templatesLoading = ref(false)
const templatesError = ref(false)
const templateUsingId = ref<string | null>(null)

async function loadTemplates() {
  templatesLoading.value = true
  templatesError.value = false
  try {
    templates.value = await listTemplates('workflow')
  } catch {
    templatesError.value = true
    message.error(t('errors.failed_to_fetch_workflow_templates'))
  } finally {
    templatesLoading.value = false
  }
}

function templateNodeCount(t: TemplateItem): number {
  return Array.isArray(t.payload?.nodes) ? t.payload.nodes.length : 0
}

function templateEdgeCount(t: TemplateItem): number {
  return Array.isArray(t.payload?.edges) ? t.payload.edges.length : 0
}

async function useWorkflowTemplate(tpl: TemplateItem): Promise<boolean> {
  templateUsingId.value = tpl.id
  try {
    const resp = await useTemplate(tpl.id)
    // 兼容直接返回 {payload,...} 或 {data:{payload,...}} 包装
    const body = resp?.data && typeof resp.data === 'object' && resp.data.payload ? resp.data : resp
    const payload = body?.payload
    if (!payload || !Array.isArray(payload.nodes)) throw new Error(t('common.incomplete_template_data'))
    // 替换当前画布：模板只加载不落库，可编辑后手动保存
    resetCanvas()
    fromBackendFormat({ name: body?.name || tpl.name, nodes: payload.nodes, edges: payload.edges || [] })
    message.success(t('已加载模板「{name}」，可编辑后保存', { name: body?.name || tpl.name }))
    await nextTick()
    try { fitView({ padding: 0.15 }) } catch { /* 忽略布局异常 */ }
    return true
  } catch (e) {
    const err = e as { response?: { data?: { error?: string } }; message?: string }
    message.error(t('加载模板失败: {error}', { error: err?.response?.data?.error || err?.message || '' }))
    return false
  } finally {
    templateUsingId.value = null
  }
}

/** 从弹窗里点「使用」：成功才关窗（失败时保留列表，用户可换一个模板） */
async function onUseTemplate(tpl: TemplateItem) {
  if (await useWorkflowTemplate(tpl)) templateOpen.value = false
}

// ── Mount ──
onMounted(() => {
  loadWorkflows()
  loadInstances()
  loadAgentList()
  loadTemplates()
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  stopStatusPolling()
  window.removeEventListener('keydown', onKeydown)
})

function statusClass(nodeProps: any): string {
  return `status-${nodeProps.data?.execStatus || 'idle'}`
}
</script>

<template>
  <div class="workflow-container">
    <!-- Toolbar -->
    <div class="toolbar">
      <div class="toolbar-left">
        <Input
          v-model:value="workflowName"
          style="width: 200px"
          size="small"
        />
        <Button
          size="small"
          type="primary"
          @click="saveWorkflow"
        >
          <template #icon>
            <SaveOutlined />
          </template>
          {{ $t('common.save') }}
        </Button>
        <Button
          size="small"
          type="primary"
          ghost
          :disabled="isExecuting"
          @click="executeWorkflow"
        >
          <template #icon>
            <PlayCircleOutlined />
          </template>
          {{ isExecuting ? $t('common.running') : $t('common.execute') }}
        </Button>
        <Button
          size="small"
          :title="$t('workflow.run_this_workflow_in_the_current_conversation')"
          @click="runInChat"
        >
          <template #icon>
            <MessageOutlined />
          </template>
          {{ $t('chat.run_to_conversation') }}
        </Button>
        <Button
          size="small"
          :title="$t('workflow.persistently_assemble_this_workflow_into_an_agent_attached_on_every_dispatch')"
          @click="attachToAgentOpen = true"
        >
          {{ $t('agent.assemble_to_agent') }}
        </Button>
        <Button
          size="small"
          :title="$t('common.auto_layout_by_layer')"
          @click="autoLayout"
        >
          <template #icon>
            <AlignCenterOutlined />
          </template>
        </Button>
        <Button
          size="small"
          :title="$t('workflow.copy_selected_node_ctrl_d')"
          :disabled="!selectedNode"
          @click="duplicateSelectedNode"
        >
          <template #icon>
            <CopyOutlined />
          </template>
        </Button>
        <Button
          size="small"
          @click="resetCanvas"
        >
          {{ $t('common.create_2') }}
        </Button>
      </div>
      <div class="toolbar-right">
        <Button
          size="small"
          @click="templateOpen = true"
        >
          <template #icon>
            <RocketOutlined />
          </template>
          {{ $t('common.template') }}
        </Button>
        <Button
          size="small"
          @click="showDrawer = true"
        >
          <template #icon>
            <UnorderedListOutlined />
          </template>
          {{ $t('workflow.workflow_list') }}
        </Button>
        <Button
          size="small"
          @click="showHistory = !showHistory"
        >
          <template #icon>
            <HistoryOutlined />
          </template>
          {{ $t('common.execution_history') }}
        </Button>
        <Tag
          v-if="workflowId"
          color="success"
        >
          {{ $t('common.saved') }}
        </Tag>
        <Tag
          v-else
          color="warning"
        >
          {{ $t('common.unsaved') }}
        </Tag>
      </div>
    </div>

    <div class="main-area">
      <!-- Left: Node Palette -->
      <div class="node-palette">
        <div class="palette-title">
          {{ $t('workflow.node') }}
        </div>
        <div
          v-for="nt in nodeTypes"
          :key="nt.type"
          class="palette-item"
          :draggable="true"
          @dragstart="(e: DragEvent) => onDragStart(e, nt.type)"
        >
          <span class="palette-icon">{{ nt.icon }}</span>
          <span class="palette-label">{{ nt.label }}</span>
        </div>
        <div class="palette-hint">
          {{ $t('common.drag_onto_canvas') }}<br>{{ $t('common.shift_multi_select') }}<br>{{ $t('common.delete_2') }}
        </div>
      </div>

      <!-- Center: Canvas -->
      <div
        class="canvas-wrapper"
        @drop="onDrop"
        @dragover="onDragOver"
      >
        <VueFlow
          v-model:nodes="nodes"
          v-model:edges="edges"
          :default-edge-options="{ type: 'smoothstep', animated: true }"
          :snap-to-grid="true"
          :snap-grid="[15, 15]"
          fit-view-on-init
          @node-click="onNodeClick"
          @pane-click="onPaneClick"
          @connect="onConnect"
        >
          <Background
            :gap="15"
            :size="1"
          />
          <Controls />
          <MiniMap />

          <template #node-input="nodeProps">
            <div
              class="custom-node"
              :class="statusClass(nodeProps)"
              :style="{ borderColor: 'var(--success)' }"
            >
              <div
                class="node-header"
                style="background: var(--node-green-fill);"
              >
                <span>📥 {{ nodeProps.data?.label || $t('common.input') }}</span>
              </div>
              <div class="node-body">
                <span class="node-type-tag">input</span>
              </div>
              <Handle
                type="source"
                :position="Position.Bottom"
              />
            </div>
          </template>

          <template #node-llm="nodeProps">
            <div
              class="custom-node"
              :class="statusClass(nodeProps)"
              :style="{ borderColor: 'var(--node-violet)' }"
            >
              <Handle
                type="target"
                :position="Position.Top"
              />
              <div
                class="node-header"
                style="background: var(--node-violet-fill);"
              >
                <span>🧠 {{ nodeProps.data?.label || 'LLM' }}</span>
              </div>
              <div class="node-body">
                <span class="node-type-tag">llm</span>
                <span
                  v-if="nodeProps.data?.config?.model"
                  class="node-detail"
                >{{ nodeProps.data.config.model }}</span>
              </div>
              <Handle
                type="source"
                :position="Position.Bottom"
              />
            </div>
          </template>

          <template #node-tool="nodeProps">
            <div
              class="custom-node"
              :class="statusClass(nodeProps)"
              :style="{ borderColor: 'var(--info)' }"
            >
              <Handle
                type="target"
                :position="Position.Top"
              />
              <div
                class="node-header"
                style="background: var(--node-blue-fill);"
              >
                <span>🔧 {{ nodeProps.data?.label || $t('agent.tools') }}</span>
              </div>
              <div class="node-body">
                <span class="node-type-tag">tool</span>
                <span
                  v-if="nodeProps.data?.config?.tool_name"
                  class="node-detail"
                >{{ nodeProps.data.config.tool_name }}</span>
              </div>
              <Handle
                type="source"
                :position="Position.Bottom"
              />
            </div>
          </template>

          <template #node-skill="nodeProps">
            <div
              class="custom-node"
              :class="statusClass(nodeProps)"
              :style="{ borderColor: 'var(--node-pink)' }"
            >
              <Handle
                type="target"
                :position="Position.Top"
              />
              <div
                class="node-header"
                style="background: var(--node-pink-fill);"
              >
                <span>🎯 {{ nodeProps.data?.label || $t('agent.skill') }}</span>
              </div>
              <div class="node-body">
                <span class="node-type-tag">skill</span>
                <span
                  v-if="nodeProps.data?.config?.skill_name"
                  class="node-detail"
                >{{ nodeProps.data.config.skill_name }}</span>
              </div>
              <Handle
                type="source"
                :position="Position.Bottom"
              />
            </div>
          </template>

          <template #node-knowledge="nodeProps">
            <div
              class="custom-node"
              :class="statusClass(nodeProps)"
              :style="{ borderColor: 'var(--node-teal)' }"
            >
              <Handle
                type="target"
                :position="Position.Top"
              />
              <div
                class="node-header"
                style="background: var(--node-teal-fill);"
              >
                <span>📚 {{ nodeProps.data?.label || $t('knowledge.knowledge_base') }}</span>
              </div>
              <div class="node-body">
                <span class="node-type-tag">knowledge</span>
                <span
                  v-if="nodeProps.data?.config?.kb_id"
                  class="node-detail"
                >{{ nodeProps.data.config.kb_id }}</span>
              </div>
              <Handle
                type="source"
                :position="Position.Bottom"
              />
            </div>
          </template>

          <template #node-agent="nodeProps">
            <div
              class="custom-node"
              :class="statusClass(nodeProps)"
              :style="{ borderColor: 'var(--node-indigo)' }"
            >
              <Handle
                type="target"
                :position="Position.Top"
              />
              <div
                class="node-header"
                style="background: var(--node-indigo-fill);"
              >
                <span>🤖 {{ nodeProps.data?.label || 'Agent' }}</span>
              </div>
              <div class="node-body">
                <span class="node-type-tag">agent</span>
                <span
                  v-if="nodeProps.data?.config?.name"
                  class="node-detail"
                >{{ nodeProps.data.config.name }}</span>
              </div>
              <Handle
                type="source"
                :position="Position.Bottom"
              />
            </div>
          </template>

          <template #node-condition="nodeProps">
            <div
              class="custom-node"
              :class="statusClass(nodeProps)"
              :style="{ borderColor: 'var(--warning)' }"
            >
              <Handle
                type="target"
                :position="Position.Top"
              />
              <div
                class="node-header"
                style="background: var(--node-amber-fill);"
              >
                <span>🔀 {{ nodeProps.data?.label || $t('common.condition') }}</span>
              </div>
              <div class="node-body">
                <span class="node-type-tag">condition</span>
              </div>
              <Handle
                id="true"
                type="source"
                :position="Position.Bottom"
                style="left: 30%"
              />
              <Handle
                id="false"
                type="source"
                :position="Position.Bottom"
                style="left: 70%"
              />
            </div>
          </template>

          <template #node-output="nodeProps">
            <div
              class="custom-node"
              :class="statusClass(nodeProps)"
              :style="{ borderColor: 'var(--node-gray)' }"
            >
              <Handle
                type="target"
                :position="Position.Top"
              />
              <div
                class="node-header"
                style="background: var(--node-gray-fill);"
              >
                <span>📤 {{ nodeProps.data?.label || $t('common.output_2') }}</span>
              </div>
              <div class="node-body">
                <span class="node-type-tag">output</span>
              </div>
            </div>
          </template>
        </VueFlow>
      </div>

      <!-- Right: Property Panel -->
      <div
        v-if="showPanel && selectedNode"
        class="property-panel"
      >
        <div class="panel-header">
          <span>{{ $t('workflow.node_properties') }}</span>
          <Button
            type="text"
            size="small"
            @click="showPanel = false"
          >
            <template #icon>
              <CloseOutlined />
            </template>
          </Button>
        </div>
        <div class="panel-body">
          <Form
            layout="vertical"
            size="small"
          >
            <FormItem :label="$t('common.tag')">
              <Input
                v-model:value="editLabel"
                :placeholder="$t('workflow.node_label')"
              />
            </FormItem>

            <template v-if="selectedNode.data?.nodeType === 'llm'">
              <div class="section-divider" />
              <FormItem :label="$t('agent.model')">
                <Select
                  v-model:value="editModel"
                  :options="modelOptions"
                  style="width: 100%"
                  allow-clear
                />
              </FormItem>
              <FormItem label="System Prompt">
                <Input.TextArea
                  v-model:value="editSystemPrompt"
                  :rows="4"
                  :placeholder="$t('agent.system_prompt')"
                />
              </FormItem>
              <FormItem :label="$t('chat.user_message_template')">
                <Input.TextArea
                  v-model:value="editUserMessage"
                  :rows="3"
                  :placeholder="$t('使用 {ph} 引用上游输出或状态变量', { ph: '{{变量名}}' })"
                />
              </FormItem>
            </template>

            <template v-if="selectedNode.data?.nodeType === 'tool'">
              <div class="section-divider" />
              <FormItem :label="$t('agent.tool_name')">
                <Select
                  v-model:value="editToolName"
                  :options="toolOptions"
                  :placeholder="$t('agent.select_tools')"
                  style="width: 100%"
                />
              </FormItem>
              <FormItem :label="$t('errors.retry_count_on_failure')">
                <InputNumber
                  v-model:value="editRetries"
                  :min="0"
                  :max="5"
                  style="width: 100%"
                />
              </FormItem>
            </template>

            <template v-if="selectedNode.data?.nodeType === 'skill'">
              <div class="section-divider" />
              <FormItem :label="$t('agent.skill_name')">
                <Input
                  v-model:value="editSkillName"
                  :placeholder="$t('agent.installed_skill_name_e_g_greeting_summary')"
                />
              </FormItem>
              <FormItem :label="$t('agent.parameter_json')">
                <Input.TextArea
                  v-model:value="editSkillParams"
                  :rows="3"
                  placeholder="{&quot;name&quot;: &quot;Alice&quot;}"
                />
              </FormItem>
            </template>

            <template v-if="selectedNode.data?.nodeType === 'knowledge'">
              <div class="section-divider" />
              <FormItem :label="$t('knowledge.knowledge_base_id')">
                <Input
                  v-model:value="editKbId"
                  :placeholder="$t('knowledge.kb_id_view_on_knowledge_base_page')"
                />
              </FormItem>
              <FormItem :label="$t('knowledge.search_question')">
                <Input.TextArea
                  v-model:value="editKbQuery"
                  :rows="2"
                  :placeholder="$t('knowledge.blank_uses_the_upstream_output_as_the_search_term')"
                />
              </FormItem>
              <FormItem :label="$t('common.rows_returned')">
                <InputNumber
                  v-model:value="editKbTopK"
                  :min="1"
                  :max="20"
                  style="width: 100%"
                />
              </FormItem>
            </template>

            <template v-if="selectedNode.data?.nodeType === 'agent'">
              <div class="section-divider" />
              <div class="section-title">
                {{ $t('agent.agent_config') }}
              </div>
              <FormItem label="Agent">
                <Select
                  v-if="!agentManualMode"
                  v-model:value="editAgentId"
                  :options="agentOptions"
                  :loading="agentLoading"
                  :placeholder="$t('agent.select_an_installed_agent')"
                  style="width: 100%"
                  allow-clear
                  @change="onAgentSelect"
                />
                <Input
                  v-else
                  v-model:value="editAgentName"
                  :placeholder="$t('agent.manually_enter_agent_name')"
                />
                <div
                  v-if="agentManualMode && !agentLoading"
                  class="agent-hint"
                >
                  {{ $t('agent.agent_list_unavailable_switched_to_manual_input') }}
                </div>
              </FormItem>
              <FormItem :label="$t('common.name_auto_filled_overridable')">
                <Input
                  v-model:value="editAgentName"
                  :placeholder="$t('agent.agent_name')"
                />
              </FormItem>
              <FormItem :label="$t('agent.system_prompt_auto_filled_overridable')">
                <Input.TextArea
                  v-model:value="editSystemPrompt"
                  :rows="3"
                  :placeholder="$t('agent.blank_uses_the_agent_default_prompt')"
                />
              </FormItem>
              <FormItem :label="$t('agent.model_auto_filled_overridable')">
                <Select
                  v-model:value="editModel"
                  :options="agentModelOptions"
                  style="width: 100%"
                  allow-clear
                />
              </FormItem>
              <FormItem :label="$t('common.max_rounds_auto_filled_overridable')">
                <InputNumber
                  v-model:value="editMaxTurns"
                  :min="1"
                  :max="50"
                  style="width: 100%"
                />
              </FormItem>
              <FormItem :label="$t('workflow.task_input')">
                <Input.TextArea
                  v-model:value="editAgentTask"
                  :rows="3"
                  :placeholder="$t('workflow.subtask_description_supports_nodeid_to_reference_upstream_node_output_e_g_llm_1_leave_empty_to_use_the_upstream_output')"
                />
              </FormItem>
            </template>

            <template v-if="selectedNode.data?.nodeType === 'condition'">
              <div class="section-divider" />
              <FormItem :label="$t('common.condition_expression')">
                <Input.TextArea
                  v-model:value="editCondition"
                  :rows="3"
                  :placeholder="$t('common.e_g_state_status_ok_evaluated_against_upstream_output')"
                />
              </FormItem>
              <FormItem :label="$t('common.input_variable_reference')">
                <Input
                  v-model:value="editVariable"
                  :placeholder="$t('common.variable_name_empty_upstream_output')"
                />
              </FormItem>
            </template>

            <div class="section-divider" />
            <Button
              danger
              block
              @click="deleteSelectedNode"
            >
              <template #icon>
                <DeleteOutlined />
              </template>
              {{ $t('workflow.delete_node') }}
            </Button>
          </Form>
        </div>
      </div>
    </div>

    <!-- Execution logs + results bar -->
    <div
      v-if="executionLogs.length > 0 || Object.keys(executionResults).length > 0"
      class="execution-bar"
    >
      <div class="execution-header">
        <span>{{ $t('common.execution_log') }}</span>
        <Button
          type="text"
          size="small"
          class="exec-clear"
          @click="executionLogs = []; executionResults = {}"
        >
          {{ $t('common.clear') }}
        </Button>
      </div>
      <div class="execution-logs">
        <div
          v-for="(log, i) in executionLogs"
          :key="i"
          class="log-line"
        >
          {{ log }}
        </div>
      </div>
      <div
        v-if="Object.keys(executionResults).length"
        class="execution-results"
      >
        <div
          v-for="(r, nid) in executionResults"
          :key="nid"
          class="result-item"
        >
          <div class="result-item-head">
            <span class="result-node">{{ nid }}</span>
            <Tag
              :color="r.status === 'completed' ? 'success' : 'error'"
              class="result-status"
            >
              {{ r.status }}
            </Tag>
          </div>
          <pre class="result-output">{{ typeof r.output === 'string' ? r.output : JSON.stringify(r.output, null, 2) }}</pre>
        </div>
      </div>
    </div>

    <!-- History drawer -->
    <Drawer
      v-if="showHistory"
      :open="showHistory"
      :title="$t('common.execution_history')"
      placement="right"
      :width="420"
      @update:open="showHistory = $event"
    >
      <Empty
        v-if="instances.length === 0"
        :description="$t('common.no_execution_records_yet')"
      />
      <div
        v-else
        class="history-list"
      >
        <div
          v-for="inst in instances"
          :key="inst.id"
          class="history-item"
        >
          <div class="history-head">
            <Tag :color="inst.status === 'completed' ? 'success' : inst.status === 'error' ? 'error' : 'processing'">
              {{ inst.status }}
            </Tag>
            <span class="history-name">{{ inst.workflow_name }}</span>
            <span class="history-time">{{ formatDate(inst.created_at) }}</span>
          </div>
          <div
            v-if="inst.error"
            class="history-error"
          >
            {{ inst.error }}
          </div>
          <div class="history-results">
            <div
              v-for="(r, nid) in inst.results"
              :key="nid"
              class="history-result"
            >
              <span class="history-node">{{ nid }}</span>
              <pre class="history-output">{{ typeof r.output === 'string' ? r.output.slice(0, 200) : JSON.stringify(r.output).slice(0, 200) }}</pre>
            </div>
          </div>
          <div class="history-actions">
            <Button
              size="small"
              @click="continueInChat(inst)"
            >
              {{ $t('chat.continue_in_the_conversation') }}
            </Button>
          </div>
        </div>
      </div>
    </Drawer>

    <!-- Workflow List Drawer -->
    <Drawer
      v-if="showDrawer"
      :open="showDrawer"
      :title="$t('workflow.saved_workflow')"
      placement="right"
      :width="380"
      @update:open="showDrawer = $event"
    >
      <Empty
        v-if="savedWorkflows.length === 0"
        :description="$t('workflow.no_workflows_yet')"
      />
      <div
        v-else
        class="workflow-list"
      >
        <div
          v-for="wf in savedWorkflows"
          :key="wf.id"
          class="workflow-item"
          role="button"
          tabindex="0"
          @click="loadWorkflow(wf); showDrawer = false"
          @keydown.enter.prevent="loadWorkflow(wf); showDrawer = false"
          @keydown.space.prevent="loadWorkflow(wf); showDrawer = false"
        >
          <div class="wf-item-info">
            <div class="wf-item-name">
              {{ wf.name || $t('workflow.untitled_workflow') }}
            </div>
            <div class="wf-item-time">
              {{ formatDate(wf.updated_at) || formatDate(wf.created_at) }}
            </div>
            <div
              v-if="agentsUsingWorkflow(wf.id).length"
              class="wf-item-time"
            >
              {{ $t('被 {n} 个 Agent 装配', { n: agentsUsingWorkflow(wf.id).length }) }}
            </div>
          </div>
          <Popconfirm
            :title="$t('common.confirm_delete')"
            @confirm="deleteWorkflow(wf.id)"
          >
            <Button
              type="text"
              danger
              size="small"
              @click.stop
            >
              <template #icon>
                <DeleteOutlined />
              </template>
            </Button>
          </Popconfirm>
        </div>
      </div>
    </Drawer>

    <!-- 模板市场：一键把模板加载进画布（只加载不落库，可编辑后手动保存） -->
    <Modal
      v-model:open="templateOpen"
      :title="$t('workflow.workflow_templates')"
      :footer="null"
      width="640px"
    >
      <PageSkeleton v-if="templatesLoading" />
      <EmptyState
        v-else-if="templatesError"
        :description="$t('errors.template_load_failed_please_retry_later')"
      />
      <EmptyState
        v-else-if="!templates.length"
        :description="$t('common.no_templates_available')"
      />
      <ul v-else class="template-list">
        <li
          v-for="tpl in templates"
          :key="tpl.id"
          class="template-item"
        >
          <div class="template-meta">
            <span class="template-name">{{ tpl.name }}</span>
            <span class="template-count">
              {{ $t('{nodes} 个节点 · {edges} 条连线', { nodes: templateNodeCount(tpl), edges: templateEdgeCount(tpl) }) }}
            </span>
          </div>
          <Button
            size="small"
            type="primary"
            :loading="templateUsingId === tpl.id"
            @click="onUseTemplate(tpl)"
          >
            {{ $t('common.use') }}
          </Button>
        </li>
      </ul>
    </Modal>

    <!-- 运行输入：图里有 input 节点时收集（见 needsRunInput 的说明） -->
    <Modal
      v-model:open="runInputOpen"
      :title="$t('common.run_input')"
      :ok-text="$t('common.run')"
      :cancel-text="$t('common.cancel')"
      @ok="confirmRunInput"
    >
      <p class="run-input-hint">
        {{ $t('workflow.this_text_becomes_the_output_of_the_graph_s_input_node_consumed_by_downstream_nodes_e_g_the_knowledge_base_query_leave_empty_to_let_each_node_use_its_own_config') }}
      </p>
      <Input.TextArea
        v-model:value="runInputValue"
        :rows="4"
        :placeholder="$t('common.enter_the_content_to_process_in_this_run')"
      />
    </Modal>

    <!-- 装配到 Agent：把该工作流持久绑定到 Agent（写回 agents.workflows） -->
    <AttachToAgentDialog
      v-model:open="attachToAgentOpen"
      kind="workflow"
      :value="workflowId || ''"
      :label="workflowName || $t('workflow.current_workflow')"
    />
  </div>
</template>

<style>
@import '@vue-flow/core/dist/style.css';
@import '@vue-flow/core/dist/theme-default.css';
@import '@vue-flow/controls/dist/style.css';
@import '@vue-flow/minimap/dist/style.css';
</style>

<style scoped>
.workflow-container { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
.toolbar { display: flex; align-items: center; justify-content: space-between; padding: 8px 16px; border-bottom: 1px solid var(--border); background: var(--bg-card); gap: 8px; flex-shrink: 0; }
.toolbar-left, .toolbar-right { display: flex; align-items: center; gap: 8px; }
.main-area { display: flex; flex: 1; overflow: hidden; position: relative; }
.node-palette { width: 168px; padding: 12px; border-right: 1px solid var(--border); background: var(--bg-secondary); flex-shrink: 0; }
.palette-title { font-weight: 600; font-size: 12px; color: var(--text-tertiary); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
.palette-item { display: flex; align-items: center; gap: 8px; padding: 8px 12px; margin-bottom: 6px; background: var(--bg-card); border: 1px solid var(--border-card); border-radius: 8px; cursor: grab; font-size: 13px; transition: box-shadow var(--dur-fast), border-color var(--dur-fast); user-select: none; }
.palette-item:hover { box-shadow: var(--shadow-md); border-color: var(--primary); }
.palette-item:active { cursor: grabbing; }
.palette-icon { font-size: 16px; }
.palette-label { font-weight: 500; color: var(--text-primary); }
.palette-hint { margin-top: 14px; font-size: 11px; line-height: 1.7; color: var(--text-tertiary); }
.canvas-wrapper { flex: 1; position: relative; }
/* 自定义节点：主题变量 + 执行状态 */
.custom-node { background: var(--bg-card); border: 2px solid; border-radius: 8px; min-width: 150px; font-size: 12px; box-shadow: var(--shadow-md); color: var(--text-primary); transition: box-shadow var(--dur-normal); }
.custom-node.status-completed { box-shadow: 0 0 0 2px var(--success), var(--shadow-lg); }
.custom-node.status-error { box-shadow: 0 0 0 2px var(--error), var(--shadow-lg); }
.custom-node.status-running { animation: nodePulse var(--dur-pulse) ease-in-out infinite; }
@keyframes nodePulse { 0%, 100% { box-shadow: 0 0 0 2px var(--info-bg); } 50% { box-shadow: 0 0 0 4px var(--info); } }
.node-header { padding: 6px 10px; border-radius: 6px 6px 0 0; font-weight: 600; font-size: 13px; white-space: nowrap; color: var(--text-primary); }
.node-body { padding: 6px 10px; display: flex; align-items: center; gap: 6px; }
.node-type-tag { background: var(--bg-secondary); padding: 1px 6px; border-radius: 4px; font-size: 10px; color: var(--text-tertiary); }
.node-detail { font-size: 10px; color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100px; }
.property-panel { width: 300px; border-left: 1px solid var(--border); background: var(--bg-card); flex-shrink: 0; overflow-y: auto; }
.panel-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 14px; color: var(--text-primary); }
.panel-body { padding: 12px; }
.section-divider { height: 1px; background: var(--border); margin: 12px 0; }
.section-title { font-weight: 600; font-size: 13px; color: var(--text-primary); margin-bottom: 8px; }
.agent-hint { margin-top: 4px; font-size: 11px; color: var(--warning); }
/* 执行日志 + 结果 */
.execution-bar { border-top: 1px solid var(--border); background: var(--bg-secondary); color: var(--text-primary); font-family: var(--font-mono); font-size: 12px; padding: 8px 16px; flex-shrink: 0; max-height: 220px; overflow-y: auto; }
.execution-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; color: var(--text-tertiary); font-size: 11px; }
.exec-clear { color: var(--text-tertiary) !important; }
.log-line { padding: 2px 0; white-space: pre-wrap; word-break: break-all; }
.execution-results { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
.result-item { border: 1px solid var(--border); border-radius: 8px; background: var(--bg-card); overflow: hidden; }
.result-item-head { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-bottom: 1px solid var(--border); }
.result-node { font-weight: 600; color: var(--text-primary); }
.result-status { margin-left: auto; }
.result-output { margin: 0; padding: 8px 10px; white-space: pre-wrap; word-break: break-word; color: var(--text-secondary); max-height: 140px; overflow-y: auto; }
/* 列表 */
.workflow-list { display: flex; flex-direction: column; gap: 8px; }
.workflow-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 12px; border: 1px solid var(--border-card); border-radius: 8px; background: var(--bg-card); cursor: pointer; transition: border-color var(--dur-fast), box-shadow var(--dur-fast); }
.workflow-item:hover { border-color: var(--primary); box-shadow: var(--shadow-md); }
.wf-item-info { flex: 1; min-width: 0; }
.wf-item-name { font-weight: 600; font-size: 14px; color: var(--text-primary); }
.wf-item-time { font-size: 12px; color: var(--text-tertiary); margin-top: 2px; }
/* 执行历史 */
.history-list { display: flex; flex-direction: column; gap: 10px; }
.history-item { border: 1px solid var(--border-card); border-radius: 8px; background: var(--bg-card); padding: 10px 12px; }
.history-head { display: flex; align-items: center; gap: 8px; }
.history-name { font-weight: 600; font-size: 13px; color: var(--text-primary); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-time { font-size: 11px; color: var(--text-tertiary); }
.history-error { margin-top: 6px; font-size: 12px; color: var(--error); }
.history-results { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
.history-result { border-top: 1px dashed var(--border); padding-top: 6px; }
.history-node { font-size: 11px; font-weight: 600; color: var(--primary); }
.history-output { margin: 4px 0 0; font-size: 11px; color: var(--text-secondary); white-space: pre-wrap; word-break: break-all; }

/* 移动端：节点面板和属性面板收窄，toolbar 允许换行；属性面板改为浮层保证画布宽度 */
@media (max-width: 768px) {
  .toolbar { flex-wrap: wrap; padding: 8px 12px; gap: 6px; }
  .toolbar-left, .toolbar-right { flex-wrap: wrap; gap: 6px; }
  .toolbar-left { flex: 1 1 100%; }
  .toolbar-left :deep(.ant-input) { width: 100% !important; }
  .node-palette { width: 120px; padding: 8px; }
  .palette-item { padding: 6px 8px; font-size: 12px; }
  .palette-hint { display: none; }
  /* 画布：保持可拖拽（min-height），画布超出时横向滚动 */
  .canvas-wrapper { min-height: 440px; }
  .canvas-wrapper::after {
    content: '↔ 可横向拖动 · 滚轮/双指缩放';
    position: absolute;
    left: 50%;
    bottom: 10px;
    transform: translateX(-50%);
    z-index: var(--z-local);
    pointer-events: none;
    padding: 4px 12px;
    border-radius: var(--radius-full);
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text-tertiary);
    font-size: 11px;
    white-space: nowrap;
    box-shadow: var(--shadow-md);
    opacity: 0.92;
  }
  /* 属性面板：浮层覆盖（不挤压画布） */
  .property-panel {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    z-index: var(--z-dropdown);
    width: min(300px, 82%);
    box-shadow: var(--shadow-lg);
  }
  .execution-bar { max-height: 140px; padding: 6px 12px; font-size: 11px; }
}

/* 超小屏：隐藏节点面板（通过 toolbar 按钮触发浮层） */
@media (max-width: 480px) {
  .node-palette { display: none; }
  .property-panel { width: 85%; max-width: 280px; }
}

@media (prefers-reduced-motion: reduce) {
  .custom-node.status-running { animation: none; }
  .palette-item, .workflow-item { transition: none; }
}
.template-list { margin: 0; padding: 0; list-style: none; }
.template-item { display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 4px; border-bottom: 1px solid var(--border-color); }
.template-item:last-child { border-bottom: none; }
.template-meta { display: flex; flex-direction: column; gap: 2px; min-inline-size: 0; }
.template-name { font-weight: 500; }
.template-count { color: var(--text-secondary); font-size: 12px; }
</style>
