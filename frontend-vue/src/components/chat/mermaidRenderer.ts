import { t } from '../../i18n'

/**
 * mermaid 图表渲染（把 ```mermaid 代码块变成 SVG）。
 *
 * 两点取舍：
 * 1. **懒加载**：mermaid 体积数百 KB，只在页面真的出现图表块时才 `import()`。
 * 2. **失败不吞内容**：渲染失败时保留原始源码并标注失败原因 —— 图表语法有误是
 *    常见情况（模型写的图偶尔不合法），此时把源码展示出来比留白有用得多。
 *
 * 加载器可注入（`setMermaidAdapter`）：渲染契约能在测试里验证，而不必依赖
 * 图库本身在 jsdom 中是否可用。
 */

export interface MermaidAdapter {
  initialize(options: Record<string, unknown>): void
  render(id: string, code: string): Promise<{ svg: string }>
}

export interface RenderOptions {
  /** 暗色主题下用 mermaid 的 dark 主题，否则图表配色与页面不搭 */
  dark: boolean
}

let injected: MermaidAdapter | null = null
let loader: Promise<MermaidAdapter> | null = null
let seq = 0

/** 测试注入替身；传 null 恢复真实懒加载 */
export function setMermaidAdapter(adapter: MermaidAdapter | null): void {
  injected = adapter
  loader = null
}

function loadAdapter(): Promise<MermaidAdapter> {
  if (injected) return Promise.resolve(injected)
  if (!loader) {
    loader = import('mermaid')
      .then(mod => ((mod as { default?: MermaidAdapter }).default ?? (mod as unknown as MermaidAdapter)))
      .catch((err) => {
        loader = null   // 允许下次重试（临时失败不该被永久放弃）
        throw err
      })
  }
  return loader
}

/**
 * 渲染 host 内尚未处理的 `.mermaid` 块。
 * 幂等：已处理的块带 `data-rendered`，重复调用不会重复渲染。
 */
export async function renderMermaidBlocks(host: HTMLElement, options: RenderOptions): Promise<void> {
  const slots = host.querySelectorAll<HTMLElement>('.mermaid:not([data-rendered])')
  if (!slots.length) return

  let mermaid: MermaidAdapter
  try {
    mermaid = await loadAdapter()
  } catch {
    return   // 加载失败：保持源码展示，不打断阅读
  }

  mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: options.dark ? 'dark' : 'default' })

  for (const slot of Array.from(slots)) {
    const code = slot.dataset.code ? decodeURIComponent(slot.dataset.code) : slot.textContent || ''
    slot.dataset.rendered = '1'
    try {
      const { svg } = await mermaid.render(`mmd-${++seq}`, code)
      slot.innerHTML = svg
      slot.classList.add('mermaid-diagram')
    } catch {
      slot.classList.add('mermaid-error')
      slot.dataset.error = t('图表语法有误，已按源码展示')
    }
  }
}
