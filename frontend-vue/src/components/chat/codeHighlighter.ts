/**
 * 代码块的按需增强：语法高亮 + 行号。
 *
 * 为什么不在 markdown 渲染时直接高亮：highlight.js 有上百 KB，而含代码的消息
 * 只占少数。这里改成「先输出带语言标记的纯代码块，挂载后再增强」：
 * - 首屏不再为用不到的高亮付出体积；
 * - 增强是纯增量，加载失败或渲染失败也只是少个配色/行号，代码本身始终可读可复制。
 *
 * 加载器可注入（`setHighlightAdapter`），增强契约因此可在测试里验证。
 */

export interface HighlightAdapter {
  getLanguage(name: string): unknown
  highlight(code: string, options: { language: string }): { value: string }
  highlightAuto(code: string): { value: string }
}

/** 需要增强的代码元素：带 data-lang 且尚未处理 */
const PENDING_SELECTOR = 'pre code[data-lang]:not([data-highlighted])'

/** 少于该行数不加行号：片段加行号是噪声，行号只在长代码里有用 */
export const LINE_NUMBER_MIN_LINES = 5

let injected: HighlightAdapter | null = null
let loader: Promise<HighlightAdapter> | null = null

/** 测试注入替身；传 null 恢复真实懒加载 */
export function setHighlightAdapter(adapter: HighlightAdapter | null): void {
  injected = adapter
  loader = null
}

function loadAdapter(): Promise<HighlightAdapter> {
  if (injected) return Promise.resolve(injected)
  if (!loader) {
    loader = import('highlight.js/lib/common')
      .then(mod => ((mod as { default?: HighlightAdapter }).default ?? (mod as unknown as HighlightAdapter)))
      .catch((err) => {
        loader = null   // 允许下次重试（临时失败不该被永久放弃）
        throw err
      })
  }
  return loader
}

/**
 * 按行切分高亮后的 HTML，并在行尾闭合、行首重开跨行的 `<span>`。
 *
 * 为什么必须做：hljs 的输出里多行字符串/注释会形成**跨行 span**，直接按 `\n`
 * 切开会让每行 HTML 不闭合（浏览器虽会容错，但行号列与代码列会错位）。
 * 这里用栈跟踪未闭合的标签，纯字符串处理，不依赖 DOM。
 */
export function splitHtmlLines(html: string): string[] {
  const lines: string[] = []
  const open: string[] = []
  let current = ''
  let i = 0

  while (i < html.length) {
    const ch = html[i]!
    if (ch === '<') {
      const end = html.indexOf('>', i)
      if (end === -1) {
        current += html.slice(i)
        break
      }
      const tag = html.slice(i, end + 1)
      current += tag
      if (/^<span[\s>]/i.test(tag)) open.push(tag)
      else if (/^<\/span/i.test(tag)) open.pop()
      i = end + 1
      continue
    }
    if (ch === '\n') {
      lines.push(current + '</span>'.repeat(open.length))
      current = open.join('')
      i++
      continue
    }
    current += ch
    i++
  }
  lines.push(current)
  return lines
}

/** 行号加在 code 内部（不改动 pre），行号本身 user-select: none，不会进复制内容 */
function addLineNumbers(block: HTMLElement): void {
  const lines = splitHtmlLines(block.innerHTML)
  if (lines.length < LINE_NUMBER_MIN_LINES) return
  block.dataset.numbered = '1'
  block.innerHTML = lines
    .map((line, index) => `<span class="code-line"><span class="code-line-no">${index + 1}</span><span class="code-line-body">${line}</span></span>`)
    .join('')
}

/** 增强 host 内尚未处理的代码块；幂等，重复调用不会重复处理 */
export async function highlightCodeBlocks(host: HTMLElement): Promise<void> {
  const blocks = host.querySelectorAll<HTMLElement>(PENDING_SELECTOR)
  if (!blocks.length) return

  let hljs: HighlightAdapter | null
  try {
    hljs = await loadAdapter()
  } catch {
    hljs = null   // 加载失败：保持无高亮的可读代码，行号仍然加
  }

  for (const block of Array.from(blocks)) {
    const lang = block.dataset.lang || ''
    const code = block.textContent || ''
    block.dataset.highlighted = '1'
    if (hljs) {
      try {
        block.innerHTML = lang && hljs.getLanguage(lang)
          ? hljs.highlight(code, { language: lang }).value
          : hljs.highlightAuto(code).value
        block.classList.add('hljs')
      } catch {
        // 高亮失败：保留原文（已经由 markdown 转义过，安全）
        block.textContent = code
        block.classList.remove('hljs')
      }
    }
    addLineNumbers(block)
  }
}
