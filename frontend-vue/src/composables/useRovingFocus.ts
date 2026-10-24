/**
 * 漫游焦点（Roving tabindex）—— CDS v1 §6.6 的落地。
 *
 * 三条核心规则，每一条都对应一个真实的失败模式：
 *  1. **Tab 必须能逃逸**：焦点在组内时按 Tab / Shift+Tab 必须交给浏览器正常移出。
 *     拦截它就会形成键盘陷阱（WCAG 2.1.2，A 级失败）—— 组件里刻意对 Tab 直接 return。
 *  2. 方向键在「可聚焦项」之间移动，只有当前项 `tabIndex=0`、其余 `-1`：
 *     整组只占一个 Tab 停靠点，键盘用户不必逐个 Tab 穿过一堆卡片。
 *  3. RTL（阿语/希伯来语）下左右键语义镜像 —— 否则"右 = 下一个"在从右往左的界面里是反的。
 *
 * 这里只放纯函数（便于单测）；组件负责取元素、把焦点交出去。
 */
export type RovingDir = 'ltr' | 'rtl'
export type RovingIntent = 'prev' | 'next' | 'first' | 'last'

/** 键位 → 语义。手册的映射表原样落地。 */
export const ROVING_KEY_MAP: Readonly<Record<string, RovingIntent>> = {
  ArrowLeft: 'prev',
  ArrowUp: 'prev',
  ArrowRight: 'next',
  ArrowDown: 'next',
  PageUp: 'first',
  Home: 'first',
  PageDown: 'last',
  End: 'last',
}

/** 当前书写方向（读 `<html dir>`；i18n 的 applyDocumentLocale 会设置它）。 */
export function documentDir(): RovingDir {
  if (typeof document === 'undefined') return 'ltr'
  return document.documentElement.getAttribute('dir') === 'rtl' ? 'rtl' : 'ltr'
}

/** 解析按键语义；RTL 下 ArrowLeft / ArrowRight 互换，上下键不变。 */
export function resolveRovingIntent(key: string, dir: RovingDir = 'ltr'): RovingIntent | null {
  let k = key
  if (dir === 'rtl') {
    if (key === 'ArrowLeft') k = 'ArrowRight'
    else if (key === 'ArrowRight') k = 'ArrowLeft'
  }
  return ROVING_KEY_MAP[k] ?? null
}

/** 下一个焦点下标。loop=true 首尾回绕，否则夹在两端。空组返回 -1。 */
export function nextRovingIndex(
  current: number,
  total: number,
  intent: RovingIntent,
  loop = true,
): number {
  if (total <= 0) return -1
  let i = current
  if (intent === 'first') i = 0
  else if (intent === 'last') i = total - 1
  else if (intent === 'prev') i = current - 1
  else i = current + 1
  if (loop) return ((i % total) + total) % total
  return Math.max(0, Math.min(total - 1, i))
}

/** 某个下标此刻应该是 0（当前停靠点）还是 -1（可被方向键到达、但不进 Tab 序列）。 */
export function rovingTabIndex(index: number, activeIndex: number): 0 | -1 {
  return index === activeIndex ? 0 : -1
}
