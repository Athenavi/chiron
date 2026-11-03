/**
 * 媒体页的**键盘快捷键解析**（纯函数，无 Vue / DOM 依赖）。
 *
 * 为什么单独抽出来：
 *
 * 1. **最容易出的错是"在不该触发的时候触发"**。用户在重命名输入框里按 Delete，
 *    删掉的却是一整批文件 —— 这类缺陷不会抛异常，只会丢数据。而"当前焦点在不在
 *    输入框里"是个纯判断，理应有测试。
 * 2. **修饰键组合的判定很细**：`Ctrl+Shift+C` 不该等于 `Ctrl+C`（用户可能在做别的），
 *    mac 的 `Cmd` 要等价于 `Ctrl`，`Alt+Enter` 不能误命中 `Enter`。
 * 3. 同一个动作有三种入口（右键菜单、快捷键、工具栏），三者必须走**同一套可用性
 *    判断**，否则会出现"菜单里灰着、但快捷键能触发"。
 */

import { canRun, type MenuAction, type MenuModel } from './menuModel'

/** 快捷键解析的结果。 */
export type ShortcutResult =
  /** 命中某动作（`preventDefault` 由调用方执行） */
  | { kind: 'action'; action: MenuAction }
  /** 命中但当前不可执行（仍需 preventDefault，否则浏览器会做出别的事，如 F5 刷新页面） */
  | { kind: 'blocked'; action: MenuAction }
  /** Esc 取消选择/关闭浮层 */
  | { kind: 'escape' }
  /** 未命中任何快捷键 */
  | { kind: 'none' }

/** 判断快捷键时需要的上下文。 */
export interface ShortcutContext {
  /** 焦点是否在可编辑元素里（输入框 / 文本域 / contenteditable） */
  inEditable: boolean
  /** 是否有模态框打开（打开时快捷键一律不生效，避免与对话框里的按键冲突） */
  modalOpen: boolean
  /** 菜单模型（复用同一套可用性判断） */
  model: MenuModel
  /** 当前是否有选中项（Esc 只在有选中时才拦截） */
  hasSelection: boolean
}

/**
 * 判断焦点是否落在可编辑元素上。
 *
 * 抽成独立函数是为了让它可测：`document.activeElement` 在单测里不好模拟，
 * 而传一个最小形状就够。
 */
export function isEditableTarget(target: {
  tagName?: string
  isContentEditable?: boolean
  getAttribute?: (name: string) => string | null
} | null): boolean {
  if (!target) return false
  if (target.isContentEditable) return true
  const tag = (target.tagName || '').toUpperCase()
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  // `role="textbox"` 的自定义控件（antd 的某些组件）
  return target.getAttribute?.('role') === 'textbox'
}

/** 事件里解析快捷键所需的最小形状（便于测试构造）。 */
export interface KeyEventLike {
  key: string
  ctrlKey?: boolean
  metaKey?: boolean
  shiftKey?: boolean
  altKey?: boolean
}

/**
 * 解析一次按键。
 *
 * 判定顺序刻意是"先排除、再匹配"：
 *  1. 模态框打开 → 全部不响应（对话框自己的按键处理优先）；
 *  2. 焦点在可编辑元素 → 只保留 Esc（用户正在输入时不该被删文件）；
 *  3. 逐个匹配。
 */
export function resolveShortcut(
  event: KeyEventLike,
  ctx: ShortcutContext,
): ShortcutResult {
  // ① 模态框打开时，键盘属于对话框。若不这样，用户在"删除确认"里按 Delete
  //    会再次触发删除，绕过确认流程。
  if (ctx.modalOpen) return { kind: 'none' }

  const key = event.key
  const isEscape = key === 'Escape'

  // ② 可编辑元素内：除了 Esc 一律不响应。
  //    Esc 允许通过是因为它同时用于"取消重命名"，由调用方决定怎么处理。
  if (ctx.inEditable && !isEscape) return { kind: 'none' }

  if (isEscape) {
    return ctx.hasSelection ? { kind: 'escape' } : { kind: 'none' }
  }

  const action = matchAction(event)
  if (!action) return { kind: 'none' }
  return canRun(ctx.model, action)
    ? { kind: 'action', action }
    : // 命中但不可执行：仍要 preventDefault。例如 F5 在"不可刷新"时若不拦截，
      // 浏览器会把整页重载掉 —— 用户看到的是应用重新加载，与操作意图毫无关系。
      { kind: 'blocked', action }
}

/**
 * 匹配"按键 → 动作"。
 *
 * 注意 `Ctrl+Shift+X` 这类组合**不**匹配 `Ctrl+X`：多按一个修饰键通常意味着
 * 用户想表达别的意图（或只是误触），此时执行剪切会让源文件意外消失。
 */
function matchAction(event: KeyEventLike): MenuAction | null {
  const add = !!(event.ctrlKey || event.metaKey)
  const shift = !!event.shiftKey
  const alt = !!event.altKey
  const key = event.key

  // Alt+Enter：属性。必须排在裸 Enter 之前判断（本函数里没有裸 Enter，保留顺序说明）
  if (alt && key === 'Enter') {
    return !add && !shift ? 'properties' : null
  }
  // 带 Alt 的组合一律不落到下面的单键分支
  if (alt) return null

  if (add) {
    if (shift) return null // Ctrl+Shift+* 不映射到任何单键动作
    switch (key.toLowerCase()) {
      case 'a': return 'select-all'
      case 'c': return 'copy'
      case 'x': return 'cut'
      case 'v': return 'paste'
      default: return null
    }
  }

  if (shift) return null // Shift+Delete 等不映射（避免与"永久删除"语义混淆）

  switch (key) {
    case 'F2': return 'rename'
    case 'Delete': return 'delete'
    case 'F5': return 'refresh'
    default: return null
  }
}

/**
 * 是否需要 `preventDefault`。
 *
 * `blocked` 也必须拦截：F2/F5/Delete 都是浏览器的默认行为（重命名无、刷新有、
 * Delete 在部分场景下会后退页面），不拦截就会出现"快捷键没生效，但页面动了"。
 */
export function shouldPreventDefault(result: ShortcutResult): boolean {
  return result.kind === 'action' || result.kind === 'blocked' || result.kind === 'escape'
}
