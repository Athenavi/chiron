import { t } from '../i18n'

/**
 * 把会话导出成 Markdown —— 用于「对话 → 知识库」的沉淀。
 *
 * 只取正文（`kind === 'text'`）：思考与工具卡片的运行细节对知识库没有价值，
 * 混进去只会污染检索结果。
 */

import type { ChatItem, TextItem } from '../components/chat/chat-types'

/** 界面上的角色名，导出后是给人读的文档，不用 'user'/'assistant' */
const ROLE_HEADING: Record<TextItem['role'], string> = {
  user: t('## 用户'),
  assistant: t('## 助手'),
}

export function sessionToMarkdown(items: readonly ChatItem[], title?: string): string {
  const blocks: string[] = []
  const heading = (title || '').trim()
  if (heading) blocks.push(`# ${heading}`)

  for (const item of items) {
    if (item.kind !== 'text') continue
    const text = (item as TextItem).content?.trim()
    if (!text) continue
    blocks.push(ROLE_HEADING[(item as TextItem).role] || t('## 消息'), text)
  }

  if (blocks.length === 0) return ''
  return `${blocks.join('\n\n')}\n`
}
