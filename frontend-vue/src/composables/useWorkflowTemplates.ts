/**
 * 工作流**模板市场**（从 `WorkflowView.vue` 抽出的第二簇）。
 *
 * 边界：模板列表状态 + 加载 + 一键使用（加载进画布、不落库）。画布回填通过**回调注入**
 * （`resetCanvas` / `fromBackendFormat` / `fitView`），composable 不直接碰画布状态。
 *
 * 来历（原文保留）：这块曾是**未接线的实现** —— 数据加载在 `onMounted` 里每页跑一次、
 * 结果丢弃，`useWorkflowTemplate` 从不被调用（见路线图 L3-6）。现在由工具栏的「模板」入口驱动。
 */
import { nextTick, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { message } from 'ant-design-vue'

import { listTemplates, useTemplate } from '../api'
import type { TemplateItem } from '../api'
import { errorDetail } from '../utils/apiError'

export interface WorkflowTemplateDeps {
  /** 清空画布（使用模板前先重置，避免与当前图混在一起） */
  resetCanvas: () => void
  /** 把后端图定义回填到画布 */
  fromBackendFormat: (data: unknown) => void
  /** `useVueFlow().fitView`：模板回填后自适应视图 */
  fitView: (options?: { padding?: number }) => unknown
}

export function useWorkflowTemplates(deps: WorkflowTemplateDeps) {
  const { t } = useI18n()

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

  function templateNodeCount(tpl: TemplateItem): number {
    return Array.isArray(tpl.payload?.nodes) ? tpl.payload.nodes.length : 0
  }

  function templateEdgeCount(tpl: TemplateItem): number {
    return Array.isArray(tpl.payload?.edges) ? tpl.payload.edges.length : 0
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
      deps.resetCanvas()
      deps.fromBackendFormat({ name: body?.name || tpl.name, nodes: payload.nodes, edges: payload.edges || [] })
      message.success(t('common.loaded_template_name_edit_and_save', { name: body?.name || tpl.name }))
      await nextTick()
      try { deps.fitView({ padding: 0.15 }) } catch { /* 忽略布局异常 */ }
      return true
    } catch (e) {
      message.error(t('errors.failed_to_load_template_error', { error: errorDetail(e, '') }))
      return false
    } finally {
      templateUsingId.value = null
    }
  }

  /** 从弹窗里点「使用」：成功才关窗（失败时保留列表，用户可换一个模板） */
  async function onUseTemplate(tpl: TemplateItem) {
    if (await useWorkflowTemplate(tpl)) templateOpen.value = false
  }

  return {
    templateOpen,
    templates,
    templatesLoading,
    templatesError,
    templateUsingId,
    loadTemplates,
    templateNodeCount,
    templateEdgeCount,
    onUseTemplate,
  }
}
