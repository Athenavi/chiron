/**
 * `/models` 页的分组：**provider → 模型名前缀**。
 *
 * 为什么要有前缀这一层：聚合型 provider（openrouter、siliconflow、本地 ollama…）
 * 一个 provider 下挂着几百个模型，只按 provider 分组会得到一个几百行的巨型组，
 * 而它们的名字本身就带着家族信息 —— `anthropic/claude-*`、`openai/gpt-*`、
 * `deepseek-chat`/`deepseek-reasoner`。按这个名字前缀再切一刀，组内才是"同类"。
 *
 * 纯函数、不碰组件状态：折叠态、选中态都由调用方持有，这里只负责"怎么分"。
 */
import type { AdminModel } from '@/api/modelAdmin'

/**
 * 同一前缀至少要有这么多个模型，才值得单独成组。
 *
 * 取 2 的理由：`gpt-4o` 与 `gpt-4o-mini` 之外，绝大多数名字的前缀是**独一份**的
 * （`claude-sonnet-4-20250514` + `claude-opus-4` 才会凑出 `claude`）。
 * 阈值设成 1 的话，一个 100 模型的 provider 会碎成几十个单行子组，标题比内容还多。
 */
export const MIN_SUBGROUP_SIZE = 2

/** 「其他」子组的 label：前缀为空、或该前缀不足以成组的模型都并到这里 */
export const OTHER_SUBGROUP_LABEL = ''

export interface ModelSubgroup {
  /** 折叠态的键，需在整个页面内唯一（provider 内唯一即可，故带上 provider） */
  key: string
  /** 显示用的前缀；空串表示「其他」组，由调用方本地化 */
  label: string
  models: AdminModel[]
  /** 子组内已启用的数量，供组头显示「共 N 个 · 已启用 M」 */
  enabledCount: number
}

export interface ModelGroup {
  key: string
  label: string
  subgroups: ModelSubgroup[]
  models: AdminModel[]
  enabledCount: number
}

/**
 * 取模型名里"家族"那一段：
 *  - 有 `/`（聚合 provider 的 vendor 前缀，如 `anthropic/claude-3-5-sonnet`）取第一段；
 *  - 否则取第一个 `-` 之前的部分（如 `deepseek-chat` → `deepseek`）；
 *  - 都没有（或分隔符就在开头，`/x` 这种）返回空串 ⇒ 归入「其他」。
 *
 * 只认第一个分隔符是刻意的：`anthropic/claude-3-5-sonnet` 的家族是 `anthropic`，
 * 而不是 `claude` —— 越切越细只会把同一个家族拆散。
 */
export function modelNamePrefix(name: string): string {
  const slash = name.indexOf('/')
  if (slash > 0) return name.slice(0, slash)
  const dash = name.indexOf('-')
  if (dash > 0) return name.slice(0, dash)
  return ''
}

/**
 * 按 provider 分组，组内再按前缀分子组。
 *
 * 组内子组数量为 1 时，调用方不必渲染子组标题（那一层没有信息量）；
 * 这里不替调用方决定，只保证「永远至少有一个子组」——
 * 组内每个模型都能被恰好一个子组收下，不会出现渲染不到的行。
 */
export function groupModelsByProvider(models: AdminModel[]): ModelGroup[] {
  const byProvider = new Map<string, AdminModel[]>()
  for (const m of models) {
    const list = byProvider.get(m.provider)
    if (list) list.push(m)
    else byProvider.set(m.provider, [m])
  }

  const groups: ModelGroup[] = []
  for (const [provider, list] of byProvider) {
    const byPrefix = new Map<string, AdminModel[]>()
    for (const m of list) {
      const prefix = modelNamePrefix(m.name)
      const arr = byPrefix.get(prefix)
      if (arr) arr.push(m)
      else byPrefix.set(prefix, [m])
    }

    const subgroups: ModelSubgroup[] = []
    const others: AdminModel[] = []
    for (const [prefix, arr] of byPrefix) {
      if (prefix && arr.length >= MIN_SUBGROUP_SIZE) {
        subgroups.push({
          key: `${provider}/${prefix}`,
          label: prefix,
          models: arr,
          enabledCount: arr.filter(m => m.enabled).length,
        })
      } else {
        // 前缀独一份、或压根没有前缀：不值得单独立标题，统一并进「其他」
        others.push(...arr)
      }
    }
    // 前缀按字母序，便于在几百个模型里定位；「其他」恒排最后（它是兜底，不是家族）
    subgroups.sort((a, b) => a.label.localeCompare(b.label))
    if (others.length) {
      subgroups.push({
        key: `${provider}//__other__`,
        label: OTHER_SUBGROUP_LABEL,
        models: others,
        enabledCount: others.filter(m => m.enabled).length,
      })
    }

    groups.push({
      key: provider,
      label: provider,
      subgroups,
      models: list,
      enabledCount: list.filter(m => m.enabled).length,
    })
  }
  return groups
}
