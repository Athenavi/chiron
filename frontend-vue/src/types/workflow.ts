/** 工作流视图与 `useWorkflowExecution` 共用的类型（两处各自定义会漂移）。 */

/** 单个节点的执行产出（后端 `/v1/workflows/{id}/status` 的 `results[node_id]`） */
export interface NodeRunResult {
  status: string
  output: unknown
  error?: string
}

/** 一次工作流运行实例（`/v1/workflows/instances` 的元素） */
export interface InstanceRecord {
  id: string
  workflow_id: string
  workflow_name: string
  status: string
  results: Record<string, NodeRunResult>
  error?: string
  created_at: string
  updated_at: string
}
