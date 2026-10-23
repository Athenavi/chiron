/**
 * 工作流画布状态与结构操作（从 `WorkflowView.vue` 抽出的簇，L3-4）。
 *
 * 边界：Vue Flow 实例、`nodes` / `edges` / 选中态、节点 id 生成、拖拽落点、连线、
 * 自动布局、删除/复制/快捷键，以及 VueFlow ↔ 后端格式的互转。**不含**「节点配置表单」那一簇
 * （`edit*` 字段 + `onNodeClick` / `applyNodeConfig`：与 16 个表单字段强耦合，留在视图）。
 *
 * `workflowName` 与 `showPanel` 跨簇共享（前者被格式回填写、后者被选中/删除逻辑写），
 * 故由视图注入；`nodeTypes` 同时是视图模板渲染的静态表，也注入。
 *
 * `nodeCounter` 是**组件实例级**单例（`let`），被 `genNodeId` / `fromBackendFormat` /
 * `clearCanvas` 三处读写 —— 抽入本模块后语义不变（composable 每个组件实例调用一次）。
 */
import { ref } from 'vue'
import type { Ref } from 'vue'
import { useVueFlow } from '@vue-flow/core'
import type { Connection, Edge, Node } from '@vue-flow/core'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'

export interface NodeTypeDef {
  type: string
  label: string
  color: string
  icon: string
  description: string
}

export interface GraphNodeBackend {
  id: string
  label: string
  node_type: string
  config?: Record<string, unknown>
}

export interface GraphEdgeBackend {
  source_id: string
  target_id: string
  condition?: string
  label?: string
}

export interface WorkflowCanvasDeps {
  /** 节点类型表（视图模板也渲染它） */
  nodeTypes: NodeTypeDef[]
  /** 画布名称（格式回填会写它） */
  workflowName: Ref<string>
  /** 右侧配置面板可见性（选中/删除逻辑会写它） */
  showPanel: Ref<boolean>
}

export function useWorkflowCanvas(deps: WorkflowCanvasDeps) {
  const { t } = useI18n()
  const { nodeTypes, workflowName, showPanel } = deps

  // ── Vue Flow ──
  const { findNode, addNodes, addEdges, removeNodes, getNodes, getEdges, getSelectedNodes, fitView } = useVueFlow({
    defaultEdgeOptions: { type: 'smoothstep', animated: true },
    multiSelectionKeyCode: ['Shift', 'Meta', 'Control'],
  })

  // ── State ──
  const nodes = ref<Node[]>([])
  const edges = ref<Edge[]>([])
  const selectedNode = ref<Node | null>(null)
  const dragNodeType = ref<string | null>(null)

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
      const config: Record<string, unknown> = { ...(n.data?.config || {}) }
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

  function fromBackendFormat(data: unknown) {
    if (!data) return
    const graphDef = (typeof data === 'string' ? JSON.parse(data) : data) as {
      name?: string
      nodes?: GraphNodeBackend[]
      edges?: GraphEdgeBackend[]
    }
    nodes.value = (graphDef.nodes || []).map((n: GraphNodeBackend): Node => {
      const cfg = n.config || {}
      const pos = (cfg.position ?? {}) as { x?: number; y?: number }
      return {
        id: n.id,
        type: n.node_type,
        position: { x: pos.x || 0, y: pos.y || 0 },
        data: { label: n.label, nodeType: n.node_type, color: getNodeColor(n.node_type), icon: getNodeIcon(n.node_type), config: cfg },
      }
    })
    edges.value = (graphDef.edges || []).map((e: GraphEdgeBackend, i: number): Edge => ({
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

  /**
   * 清空画布。原 `resetCanvas` 一并重置 `workflowId` 与执行态（`executionResults` /
   * `executionLogs`）—— 那两项属「执行/持久化」簇，由视图在调用本函数后自行重置
   * （见 `WorkflowView.vue` 的 `resetCanvas`）。
   */
  function clearCanvas() {
    nodes.value = []
    edges.value = []
    workflowName.value = t('workflow.new_workflow')
    selectedNode.value = null
    showPanel.value = false
    nodeCounter = 0
  }

  return {
    nodes, edges, selectedNode, dragNodeType,
    findNode, addNodes, addEdges, removeNodes, getNodes, getEdges, getSelectedNodes, fitView,
    genNodeId, getNodeColor, getNodeIcon,
    onDragStart, onDragOver, onDrop, onConnect, autoLayout,
    deleteSelectedNode, deleteSelectedNodes, duplicateSelectedNode, onKeydown,
    toBackendFormat, fromBackendFormat, clearCanvas,
  }
}
