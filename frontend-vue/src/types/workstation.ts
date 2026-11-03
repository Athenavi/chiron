import { t } from '../i18n'

/**
 * 五大工作台的标识与展示文案。
 *
 * 唯一事实源是仓库根的 shared/workstations.json：TS（本文件）、
 * internal/model/workstation.go（Go）与 python-engine/app/core/capabilities.py
 * 的 WorkstationType（Python）三处声明由
 * python-engine/tests/test_workstation_contract.py 比对一致性。
 *
 * 运行时前端不读那个 JSON —— 它是比对基准，不是配置源。
 */

export const WORKSTATIONS = [
  'dialogue',
  'agent',
  'skill',
  'knowledge',
  'plugin',
] as const

export type WorkstationType = (typeof WORKSTATIONS)[number]

/**
 * 展示名。停靠坞（AppLayout）与命令面板（CommandPalette）共用这一份 ——
 * 此前两处各写一遍，同一个入口会同时出现两种叫法。
 */
export const WORKSTATION_LABELS: Record<WorkstationType, string> = {
  dialogue: t('chat.chat'),
  agent: 'Agent',
  skill: t('agent.skill'),
  knowledge: t('knowledge.knowledge'),
  plugin: t('common.plugin'),
}

/** 一句话描述（停靠坞浮层与命令面板共用） */
export const WORKSTATION_DESCRIPTIONS: Record<WorkstationType, string> = {
  dialogue: t('chat.ai_chat_assistant'),
  agent: t('agent.multi_agent_collaboration'),
  skill: t('agent.tool_mcp'),
  knowledge: t('knowledge.rag_retrieval_augmentation'),
  plugin: t('common.extensions'),
}

/** 工作台 → 路由路径（五个入口的唯一定义） */
export const WORKSTATION_ROUTES: Record<WorkstationType, string> = {
  dialogue: '/chat',
  agent: '/agents',
  skill: '/skills',
  knowledge: '/knowledge',
  plugin: '/plugins',
}

/**
 * 判定任意字符串是否为已知工作台标识。
 *
 * 后端字段（如 `GET /v1/capabilities` 的 `workstation_type`）走这里收窄类型，
 * 而不是各自写 `|| 'other'` 这类兜底 —— 后者会把契约外的值悄悄带进渲染逻辑。
 */
export function isWorkstation(value: unknown): value is WorkstationType {
  return typeof value === 'string' && (WORKSTATIONS as readonly string[]).includes(value)
}
