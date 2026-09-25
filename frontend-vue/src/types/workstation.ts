import { t } from '../i18n'

/**
 * 六大工作台的标识与展示文案。
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
  'workflow',
  'skill',
  'knowledge',
  'plugin',
] as const

export type WorkstationType = (typeof WORKSTATIONS)[number]

/**
 * 展示名。停靠坞（AppLayout）与命令面板（CommandPalette）共用这一份 ——
 * 此前两处各写一遍，`/workflow` 在 AppLayout 里同时叫「工作台」和「工作流」。
 */
export const WORKSTATION_LABELS: Record<WorkstationType, string> = {
  dialogue: t('对话'),
  agent: 'Agent',
  workflow: t('工作流'),
  skill: t('技能'),
  knowledge: t('知识'),
  plugin: t('插件'),
}

/** 一句话描述（停靠坞浮层与命令面板共用） */
export const WORKSTATION_DESCRIPTIONS: Record<WorkstationType, string> = {
  dialogue: t('智能对话助手'),
  agent: t('多智能体协同'),
  workflow: t('DAG 流程编排'),
  skill: t('工具 MCP'),
  knowledge: t('RAG 检索增强'),
  plugin: t('扩展能力'),
}

/** 工作台 → 路由路径（六个入口的唯一定义） */
export const WORKSTATION_ROUTES: Record<WorkstationType, string> = {
  dialogue: '/chat',
  agent: '/agents',
  workflow: '/workflow',
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
