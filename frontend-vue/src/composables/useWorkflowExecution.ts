/**
 * 工作流**执行与状态轮询**（从 `WorkflowView.vue` 抽出的第一簇）。
 *
 * 边界刻意划在"提交运行 → 轮询状态 → 落到画布与日志 → 刷新运行历史"这条链上：
 * 这一簇状态（`isExecuting` / `executionLogs` / `executionResults` / `instances`）只被它读写，
 * 所以用**依赖注入**把视图拥有的 ref 传进来，而不去碰视图的其它局部状态。
 *
 * 视图侧只需把返回的名字解构出来（模板与既有函数里的名字保持不变），
 * 因此这次抽取的可见行为应当**逐字不变** —— 由 `WorkflowView.spec.ts` 兜底。
 */
import { computed, ref, type ComputedRef, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { message } from 'ant-design-vue'
import type { Node } from '@vue-flow/core'

import { api } from '../api'
import { errorDetail } from '../utils/apiError'
import type { InstanceRecord, NodeRunResult } from '../types/workflow'

export interface WorkflowExecutionDeps {
  /** 已保存工作流的 id；未保存时为 null（此时不允许直接运行） */
  workflowId: Ref<string | null>
  /** 画布节点（`useVueFlow().getNodes`） */
  getNodes: ComputedRef<Node[]>
  isExecuting: Ref<boolean>
  executionLogs: Ref<string[]>
  executionResults: Ref<Record<string, NodeRunResult>>
  /** 运行历史列表 */
  instances: Ref<InstanceRecord[]>
}

const POLL_INTERVAL_MS = 2000

export function useWorkflowExecution(deps: WorkflowExecutionDeps) {
  const { t } = useI18n()
  const { workflowId, getNodes, isExecuting, executionLogs, executionResults, instances } = deps

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
    } catch (err) {
      isExecuting.value = false
      executionLogs.value.push(t('errors.submit_failed_error', { error: errorDetail(err, '') }))
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
          executionLogs.value.push(t('errors.failed_error', { error: data.error || '' }))
          isExecuting.value = false
          stopStatusPolling()
          await loadInstances()
        }
      } catch {
        stopStatusPolling()
        isExecuting.value = false
        executionLogs.value.push(t('errors.status_query_failed'))
      }
    }, POLL_INTERVAL_MS)
  }

  function applyExecutionStatus(data: unknown) {
    const d = (data ?? {}) as { results?: Record<string, NodeRunResult> }
    const results = d.results || {}
    executionResults.value = results
    for (const n of getNodes.value) {
      const r = results[n.id]
      n.data = { ...n.data, execStatus: r ? (r.status === 'completed' ? 'completed' : 'error') : 'idle' }
    }
    for (const [nid, r] of Object.entries(results)) {
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

  return {
    runInputOpen,
    runInputValue,
    attachToAgentOpen,
    needsRunInput,
    executeWorkflow,
    confirmRunInput,
    submitWorkflowRun,
    startStatusPolling,
    stopStatusPolling,
    applyExecutionStatus,
    loadInstances,
  }
}
