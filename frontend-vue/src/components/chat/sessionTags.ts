/**
 * 会话标签：预设、合法化与合并。
 *
 * 背景（用户报告）："目前的标签是恒定的，缺少自定义标签的功能" —— 侧栏的设置菜单里
 * 只有四个写死的标签，用户没法给自己的会话起一个贴切的标签。
 *
 * 这里把"可用标签"的定义收敛成纯函数：**预设 + 实际用过的**（预设始终可选，起到引导作用；
 * 用户新建的标签自动进入候选）。UI 只负责渲染与输入，规则可单测。
 *
 * 长度上限与后端一致（`sessions.tag` 是 varchar(64)，`PUT /v1/conversations/{id}` 也按 64 校验）——
 * 前端先拦一次，用户不用等一次 400 才知道太长。
 */

/** 预设标签：始终出现在候选里（用户没有自定义标签时也有可选项）。 */
export const PRESET_TAGS = ['工作', '学习', '项目', '其他'] as const

/** 与 `sessions.tag` / `sessions.alias` 的列宽一致 */
export const TAG_MAX_LEN = 64

/**
 * 合法化用户输入的标签：去掉首尾空白并限长。
 *
 * 返回空串表示**清除标签**（后端把空串写成 NULL，不留空字符串）。
 * 超长时按**码点**截断（而不是 UTF-16 长度），否则中文标签会被截成半个字。
 */
export function normalizeTag(raw: unknown): string {
  const text = typeof raw === 'string' ? raw.trim() : ''
  if (!text) return ''
  const chars = Array.from(text)
  return chars.length > TAG_MAX_LEN ? chars.slice(0, TAG_MAX_LEN).join('') : text
}

/**
 * 候选标签 = 预设 ∪ 已使用过的（去重、保持稳定顺序：预设在前，其余按使用顺序）。
 *
 * 为什么两边都要：只给预设 = "标签恒定"（用户报的问题）；只给已用 = 新会话第一次
 * 打标签时没有任何可选项，用户必须从零输入。
 */
export function mergeTagOptions(
  used: readonly string[],
  preset: readonly string[] = PRESET_TAGS,
): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const tag of [...preset, ...used]) {
    const value = typeof tag === 'string' ? tag.trim() : ''
    if (!value || seen.has(value)) continue
    seen.add(value)
    out.push(value)
  }
  return out
}
