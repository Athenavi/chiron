/**
 * 会话标签：预设、合法化与合并。
 *
 * 背景（用户报告）："目前的标签是恒定的，缺少自定义标签的功能" —— 侧栏的设置菜单里
 * 只有四个写死的标签，用户没法给自己的会话起一个贴切的标签。
 *
 * 这里把"可用标签"的定义收敛成纯函数：候选 = 调用方传入的 preset ∪ **用户实际用过的**
 * 标签（用户新建的自动进入候选）。UI 只负责渲染与输入，规则可单测。
 *
 * **刻意不内置预设标签**：前端不承担业务数据。原先写死的四个标签（工作/学习/项目/其他）
 * 已移除 —— 候选完全来自用户自己的历史，新标签由用户直接输入。若将来需要"组织级预设"，
 * 应由后台配置下发（接口待建），而不是回到前端硬编码。
 *
 * 长度上限与后端一致（`sessions.tag` 是 varchar(64)，`PUT /v1/conversations/{id}` 也按 64 校验）——
 * 前端先拦一次，用户不用等一次 400 才知道太长。
 */

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
 * 候选标签 = 预设（默认空）∪ 已使用过的（去重、保持稳定顺序：预设在前，其余按使用顺序）。
 *
 * `preset` 保留为**显式传入的候选来源**（默认空）：调用方若从后台拿到组织级预设可自行传入，
 * 本纯函数不关心它从哪来。
 */
export function mergeTagOptions(
  used: readonly string[],
  preset: readonly string[] = [],
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
