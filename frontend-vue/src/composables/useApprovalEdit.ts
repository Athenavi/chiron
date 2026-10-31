/**
 * 审批卡片的「编辑参数」折叠区（C5），从 `ChatView.vue` 抽出。
 *
 * 边界：只管理**编辑态**（开合 / 文本 / 校验错误），不持有待审批队列本身，也不自己
 * 发请求 —— 提交经依赖注入的 `resolveApproval` 走既有审批通道，于是
 * "批准的就是执行的"这条不变量仍然只有一处实现，不会因为拆分多出第二份放行判断。
 *
 * 形态是**内联编辑**（复用审批卡片本身，不新开弹窗）—— 避免焦点争夺与移动端布局问题
 * （方案 01 §3.5 采纳的形态）。非法 JSON **不允许提交**：否则"批准的就是执行的"
 * 这条不变量会被半个字符串破坏。
 */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'

/** 待确认工具调用（三态栅栏"确认"态）。 */
export interface PendingApproval {
  id: string
  toolName: string
  arguments: string
  /** 审批截止时间（ms）：与后端 300s 超时对齐，用于卡片倒计时与过期清理 */
  expiresAt?: number
}

export interface ApprovalEditState {
  open: boolean
  text: string
  error: string
}

export interface ApprovalEditDeps {
  /**
   * 提交审批决定。`editedArguments` 非空 ⇒ "**编辑后批准**"：回传
   * `decision: 'edit'` 与编辑后的完整参数，由引擎重跑工具分级（级别升高则不执行）。
   */
  resolveApproval: (a: PendingApproval, approved: boolean, editedArguments?: string) => Promise<void>
}

export function useApprovalEdit(deps: ApprovalEditDeps) {
  const { t } = useI18n()
  const { resolveApproval } = deps

  const approvalEdits = ref<Record<string, ApprovalEditState>>({})

  function approvalEdit(a: PendingApproval): ApprovalEditState | undefined {
    return approvalEdits.value[a.id]
  }

  /** 参数美化：解析失败就原样显示（与子 Agent 审批卡片同一取舍）。 */
  function prettyArguments(raw: string): string {
    const text = (raw || '').trim()
    if (!text) return ''
    try {
      return JSON.stringify(JSON.parse(text), null, 2)
    } catch {
      return text
    }
  }

  /** 校验编辑框内容；返回错误文案（空串 = 合法）。**实时**反馈，非法时提交按钮禁用。 */
  function validateApprovalArgs(text: string): string {
    let parsed: unknown
    try {
      parsed = JSON.parse(text.trim())
    } catch {
      return t('errors.approval_edit_invalid_json')
    }
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return t('errors.approval_edit_must_be_json_object')
    }
    return ''
  }

  function toggleApprovalEdit(a: PendingApproval) {
    const cur = approvalEdits.value[a.id]
    if (cur?.open) {
      approvalEdits.value = {
        ...approvalEdits.value,
        [a.id]: { open: false, text: cur.text, error: '' },
      }
      return
    }
    const text = cur?.text || prettyArguments(a.arguments)
    approvalEdits.value = {
      ...approvalEdits.value,
      [a.id]: { open: true, text, error: validateApprovalArgs(text) },
    }
  }

  function onApprovalEditInput(a: PendingApproval, text: string) {
    approvalEdits.value = {
      ...approvalEdits.value,
      [a.id]: { open: true, text, error: validateApprovalArgs(text) },
    }
  }

  function clearApprovalEdit(id: string) {
    if (!approvalEdits.value[id]) return
    const next = { ...approvalEdits.value }
    delete next[id]
    approvalEdits.value = next
  }

  /** 提交"编辑后批准"：非法 JSON / 非对象**一律不提交**（按钮也已禁用）。 */
  async function submitApprovalEdit(a: PendingApproval) {
    const text = approvalEdits.value[a.id]?.text || ''
    const error = validateApprovalArgs(text)
    if (error) {
      approvalEdits.value = {
        ...approvalEdits.value,
        [a.id]: { open: true, text, error },
      }
      return
    }
    await resolveApproval(a, true, JSON.stringify(JSON.parse(text.trim())))
  }

  return {
    approvalEdit,
    toggleApprovalEdit,
    onApprovalEditInput,
    clearApprovalEdit,
    submitApprovalEdit,
  }
}
