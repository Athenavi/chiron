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
  /**
   * `bindFunctions` 是 mermaid 渲染结果的**可选**一部分：图内的点击/悬浮等交互
   * 全靠它挂事件监听。官方文档（mermaid/dist/types.d.ts 的 RenderResult）明确要求
   * **在 svg 插入 DOM 之后**再调用，否则拿不到节点。
   *
   * 此前这里只声明 `{ svg }`，于是 bindFunctions 被静默丢弃 —— 图能画出来，
   * 但所有节点交互都点不动，而失败方式完全不报错（只是"没反应"）。
   */
  render(
    id: string,
    code: string,
  ): Promise<{ svg: string; bindFunctions?: (element: Element) => void }>
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
 * 二次消毒 SVG。
 *
 * mermaid 已配置 `securityLevel: 'strict'`（见下），渲染阶段会做一层过滤 —— 但那是
 * 库内部的实现细节：版本升级、配置漂移或渲染器换实现，都可能让那层过滤变松。图的内容
 * 直接来自模型输出（可被提示注入影响），所以写入 DOM 前再剥一遍：`<script>`、`on*`
 * 事件属性、以及 `javascript:` / `data:text/html` 形式的链接。
 *
 * 刻意**不**移除 `<foreignObject>`：mermaid 默认用它在节点里排 HTML 文本，移掉会让图
 * 残缺；其中残留的事件属性同样会被下面的遍历清掉。
 */
function sanitizeSvg(svg: string): string {
  const doc = new DOMParser().parseFromString(svg, 'image/svg+xml')
  if (doc.querySelector('parsererror')) return ''
  doc.querySelectorAll('script').forEach((el) => el.remove())
  doc.querySelectorAll('*').forEach((el) => {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase()
      if (name.startsWith('on')) {
        el.removeAttribute(attr.name)
        continue
      }
      if ((name === 'href' || name === 'xlink:href') &&
          /^\s*(javascript:|data:text\/html)/i.test(attr.value)) {
        el.removeAttribute(attr.name)
      }
    }
  })
  const root = doc.documentElement
  return root ? new XMLSerializer().serializeToString(root) : ''
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
      const { svg, bindFunctions } = await mermaid.render(`mmd-${++seq}`, code)
      // 二次消毒后再写入（见 sanitizeSvg）；消毒失败则退回源码展示。
      const safe = sanitizeSvg(svg)
      if (!safe) {
        slot.classList.add('mermaid-error')
        slot.dataset.error = t('errors.chart_syntax_error_showing_source')
        continue
      }
      slot.innerHTML = safe
      slot.classList.add('mermaid-diagram')
      // bindFunctions 必须在 **svg 插入 DOM 之后**调用（mermaid 官方文档的原话是
      // "necessary for adding event listeners to the elements in the svg"）：它按
      // id / class 在传入的容器里查节点再挂监听，提前调用会因为节点还不在文档里而挂空。
      //
      // 单独 try 包住、且**不影响**图表展示：图已经画出来了，交互挂不上只是少个点击，
      // 不该把它降级成"语法错误"盖住内容。这也是原来丢弃它的代价 —— 静默失败，
      // 用户只会觉得"点不动"，没有任何信号。
      if (bindFunctions) {
        try {
          bindFunctions(slot)
        } catch (err) {
          console.warn('mermaid: bindFunctions failed, diagram interactions unavailable', err)
        }
      }
    } catch {
      slot.classList.add('mermaid-error')
      slot.dataset.error = t('errors.chart_syntax_error_showing_source')
    }
  }
}
