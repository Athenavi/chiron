/**
 * 文件名的**双向中间截断**（CDS v1/02 §2.5.3）。
 *
 * 单向右截断（`text-overflow: ellipsis`）会把
 * `deep_reinforcement_learning_policy_gradient_v3.tsx` 变成
 * `deep_reinforcement_learning_pol…` —— 用户既看不出文件类型，也认不出是哪一版。
 * 官方的做法是让位只发生在**中间**：首段可省略，尾部词干与扩展名永远保留，
 * 于是显示成 `deep_reinforcement_l…cy_gradient_v3.tsx`。
 *
 * 切分逻辑放在这里（纯函数、可单测），渲染层只管把 head 交给省略、把 tail 锁成不收缩。
 */

/** 尾部保留的词干字符数：太少认不出是哪个文件，太多会把首段挤没。 */
const TAIL_KEEP = 10

export interface SplitName {
  /** 可被省略的首段（可能为空字符串 —— 短文件名整段都在 tail 里，不做无意义的切分） */
  head: string
  /** 尾部词干 + 扩展名，必须完整显示 */
  tail: string
}

export function splitFileName(name: string): SplitName {
  const raw = String(name ?? '')
  const dot = raw.lastIndexOf('.')
  // dot <= 0：没有扩展名，或名字以点开头（`.gitignore` 那种点不是扩展名分隔符）
  const hasExt = dot > 0 && dot < raw.length - 1
  const ext = hasExt ? raw.slice(dot) : ''
  const stem = hasExt ? raw.slice(0, dot) : raw
  if (stem.length <= TAIL_KEEP) return { head: '', tail: raw }
  return {
    head: stem.slice(0, stem.length - TAIL_KEEP),
    tail: stem.slice(stem.length - TAIL_KEEP) + ext,
  }
}

/**
 * 类型徽标（CDS v1/02 §2.5.4）：后缀大写、最多 4 字符 —— `TS` / `PY` / `PDF` / `CSV`。
 * 认不出后缀时给 `FILE`，而不是留空白：空白会让磁贴看起来像加载失败。
 */
export function fileTypeBadge(name: string): string {
  const raw = String(name ?? '')
  const dot = raw.lastIndexOf('.')
  const ext = dot > 0 && dot < raw.length - 1 ? raw.slice(dot + 1) : ''
  return ext ? ext.slice(0, 4).toUpperCase() : 'FILE'
}
