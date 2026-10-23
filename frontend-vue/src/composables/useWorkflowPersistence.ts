/**
 * 工作流持久化：保存 / 列表 / 载入 / 删除（从 `WorkflowView.vue` 抽出的簇，L3-4）。
 *
 * 画布读写与重置经**依赖注入**（`toBackendFormat` / `fromBackendFormat` / `resetCanvas`），
 * 本 composable 不持有画布状态；`workflowId` / `workflowName` / `executionResults` /
 * `executionLogs` 是跨簇共享的状态，也由视图注入。
 */
import { ref } from 'vue'
import type { Ref } from 'vue'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'

import { api } from '../api'
import { errorDetail } from '../utils/apiError'
import type { GraphEdgeBackend, GraphNodeBackend } from './useWorkflowCanvas'

export interface GraphRecord {
  id: string
  name: string
  graph_json: string
  created_at: string
  updated_at: string
}

export interface WorkflowPersistenceDeps {
  workflowId: Ref<string | null>
  workflowName: Ref<string>
  executionResults: Ref<Record<string, unknown>>
  executionLogs: Ref<string[]>
  toBackendFormat: () => { nodes: GraphNodeBackend[]; edges: GraphEdgeBackend[]; entry_point: string }
  fromBackendFormat: (data: unknown) => void
  resetCanvas: () => void
}

export function useWorkflowPersistence(deps: WorkflowPersistenceDeps) {
  const { t } = useI18n()
  const {
    workflowId,
    workflowName,
    executionResults,
    executionLogs,
    toBackendFormat,
    fromBackendFormat,
    resetCanvas,
  } = deps

  const savedWorkflows = ref<GraphRecord[]>([])

  // ── API: Save ──
  async function saveWorkflow() {
    const graphData = toBackendFormat()
    const payload: Record<string, unknown> = {
      id: workflowId.value || undefined,
      name: workflowName.value,
      // 归属由网关注入的 ?user_id=（取自 JWT claims）决定，请求体里的 user_id 不被采信
      graph_json: JSON.stringify({ name: workflowName.value, ...graphData }),
    }
    try {
      const resp = await api.post('/v1/graphs', payload)
      workflowId.value = resp.data?.data?.id || resp.data?.id
      message.success(t('workflow.workflow_saved'))
      await loadWorkflows()
    } catch (err) {
      message.error(t('errors.save_failed_error', { error: errorDetail(err, '') }))
    }
  }

  // ── API: Load list ──
  async function loadWorkflows() {
    try {
      const resp = await api.get('/v1/graphs')
      savedWorkflows.value = ((resp.data?.data || []) as GraphRecord[]).filter(r => r.id)
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
    message.success(t('common.loaded_name', { name: record.name }))
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
    } catch (err) {
      message.error(t('errors.delete_failed_error', { error: errorDetail(err, '') }))
    }
  }

  return { savedWorkflows, saveWorkflow, loadWorkflows, loadWorkflow, deleteWorkflow }
}
