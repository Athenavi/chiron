import { describe, expect, it } from 'vitest'

import { buildMenuModel, type MenuContext } from '../menuModel'
import {
  isEditableTarget,
  resolveShortcut,
  shouldPreventDefault,
  type KeyEventLike,
  type ShortcutContext,
} from '../shortcuts'

/**
 * 快捷键解析的用例。
 *
 * 这里最要紧的不是"F2 能重命名"，而是**不该触发的时候绝不触发**：
 * 在重命名输入框里按 Delete 删掉一整批文件、在删除确认框里按 Delete 再触发一次删除，
 * 这两类缺陷都不会抛异常，只会丢数据。
 */

const menuCtx = (override: Partial<MenuContext> = {}): MenuContext => ({
  selectedCount: 1,
  hasFolder: false,
  hasFile: true,
  clipboard: { count: 0, mode: null },
  copySupported: true,
  moveSupported: true,
  ...override,
})

const ctx = (override: Partial<ShortcutContext> = {}): ShortcutContext => ({
  inEditable: false,
  modalOpen: false,
  model: buildMenuModel(menuCtx()),
  hasSelection: true,
  ...override,
})

const key = (k: string, mods: Partial<KeyEventLike> = {}): KeyEventLike => ({ key: k, ...mods })

/** 便捷断言：解析出某动作。 */
const actionOf = (event: KeyEventLike, override: Partial<ShortcutContext> = {}) => {
  const r = resolveShortcut(event, ctx(override))
  return r.kind === 'action' ? r.action : r.kind
}

describe('isEditableTarget（焦点是否在可编辑元素）', () => {
  it('input / textarea / select 算可编辑', () => {
    expect(isEditableTarget({ tagName: 'INPUT' })).toBe(true)
    expect(isEditableTarget({ tagName: 'textarea' })).toBe(true)
    expect(isEditableTarget({ tagName: 'SELECT' })).toBe(true)
  })

  it('contenteditable 算可编辑', () => {
    expect(isEditableTarget({ tagName: 'DIV', isContentEditable: true })).toBe(true)
  })

  it('role=textbox 的自定义控件算可编辑', () => {
    expect(isEditableTarget({ tagName: 'DIV', getAttribute: (n) => (n === 'role' ? 'textbox' : null) }))
      .toBe(true)
  })

  it('普通元素不算', () => {
    expect(isEditableTarget({ tagName: 'DIV' })).toBe(false)
    expect(isEditableTarget(null)).toBe(false)
  })

  it('tagName 大小写不敏感', () => {
    expect(isEditableTarget({ tagName: 'input' })).toBe(true)
  })
})

describe('resolveShortcut（不该触发的时候绝不触发）', () => {
  it('模态框打开时一律不响应', () => {
    // 否则用户在"删除确认"里按 Delete 会再次触发删除，绕过确认流程
    for (const event of [key('Delete'), key('F2'), key('a', { ctrlKey: true })]) {
      expect(actionOf(event, { modalOpen: true })).toBe('none')
    }
  })

  it('焦点在输入框时，除 Esc 外一律不响应', () => {
    // 重命名输入框里按 Delete 删掉一整批文件 —— 这类缺陷不抛异常，只丢数据
    const inEditable = { inEditable: true }
    expect(actionOf(key('Delete'), inEditable)).toBe('none')
    expect(actionOf(key('F2'), inEditable)).toBe('none')
    expect(actionOf(key('F5'), inEditable)).toBe('none')
    expect(actionOf(key('c', { ctrlKey: true }), inEditable)).toBe('none')
  })

  it('焦点在输入框时 Esc 仍然可用（用于取消重命名）', () => {
    expect(actionOf(key('Escape'), { inEditable: true })).toBe('escape')
  })
})

describe('resolveShortcut（命中动作）', () => {
  it('F2 → 重命名', () => {
    expect(actionOf(key('F2'))).toBe('rename')
  })

  it('Delete → 删除', () => {
    expect(actionOf(key('Delete'))).toBe('delete')
  })

  it('F5 → 刷新', () => {
    expect(actionOf(key('F5'))).toBe('refresh')
  })

  it('Alt+Enter → 属性', () => {
    expect(actionOf(key('Enter', { altKey: true }))).toBe('properties')
  })

  it('Ctrl+A / C / X / V → 全选 / 复制 / 剪切 / 粘贴', () => {
    expect(actionOf(key('a', { ctrlKey: true }))).toBe('select-all')
    expect(actionOf(key('c', { ctrlKey: true }))).toBe('copy')
    expect(actionOf(key('x', { ctrlKey: true }))).toBe('cut')
    // 粘贴要求剪贴板非空 —— 默认 ctx 的剪贴板是空的，这里显式给内容，
    // 否则测的是"剪贴板为空时 blocked"，那不是本条要验证的东西
    expect(actionOf(
      key('v', { ctrlKey: true }),
      { model: buildMenuModel(menuCtx({ clipboard: { count: 1, mode: 'copy' } })) },
    )).toBe('paste')
  })

  it('剪贴板为空时 Ctrl+V 返回 blocked（快捷键与菜单同判据）', () => {
    expect(actionOf(key('v', { ctrlKey: true }))).toBe('blocked')
  })

  it('mac 的 Cmd 等价于 Ctrl', () => {
    expect(actionOf(key('c', { metaKey: true }))).toBe('copy')
  })

  it('Ctrl+A 大小写不敏感', () => {
    expect(actionOf(key('A', { ctrlKey: true }))).toBe('select-all')
  })

  it('Esc 在有选中时取消选择', () => {
    expect(actionOf(key('Escape'))).toBe('escape')
  })

  it('Esc 在无选中时不拦截（留给上层关闭浮层）', () => {
    expect(actionOf(key('Escape'), { hasSelection: false })).toBe('none')
  })
})

describe('resolveShortcut（不误命中）', () => {
  it('Ctrl+Shift+C 不当作复制', () => {
    // 多按一个修饰键通常意味着别的意图（或误触）；执行剪切会让源文件意外消失
    expect(actionOf(key('c', { ctrlKey: true, shiftKey: true }))).toBe('none')
  })

  it('Shift+Delete 不当作删除', () => {
    // 与"永久删除"语义混淆，明确不映射
    expect(actionOf(key('Delete', { shiftKey: true }))).toBe('none')
  })

  it('Alt+其他键不落到单键分支', () => {
    expect(actionOf(key('F2', { altKey: true }))).toBe('none')
    expect(actionOf(key('Delete', { altKey: true }))).toBe('none')
  })

  it('Ctrl 加 Alt 的 Enter 不当作属性', () => {
    expect(actionOf(key('Enter', { altKey: true, ctrlKey: true }))).toBe('none')
  })

  it('裸 Enter 不映射任何动作', () => {
    expect(actionOf(key('Enter'))).toBe('none')
  })

  it('未登记的键返回 none', () => {
    expect(actionOf(key('q'))).toBe('none')
    expect(actionOf(key('F9'))).toBe('none')
  })
})

describe('resolveShortcut（命中但不可执行）', () => {
  it('没有选中项时 Delete 返回 blocked（仍要阻止浏览器默认行为）', () => {
    const r = resolveShortcut(
      key('Delete'),
      ctx({ hasSelection: false, model: buildMenuModel(menuCtx({ selectedCount: 0, hasFile: false })) }),
    )
    expect(r).toEqual({ kind: 'blocked', action: 'delete' })
  })

  it('复制不可用（后端不支持）时 Ctrl+C 返回 blocked', () => {
    const r = resolveShortcut(
      key('c', { ctrlKey: true }),
      ctx({ model: buildMenuModel(menuCtx({ copySupported: false })) }),
    )
    expect(r).toEqual({ kind: 'blocked', action: 'copy' })
  })

  it('多选时 F2 返回 blocked（重命名只能单项）', () => {
    const r = resolveShortcut(
      key('F2'),
      ctx({ model: buildMenuModel(menuCtx({ selectedCount: 3 })) }),
    )
    expect(r).toEqual({ kind: 'blocked', action: 'rename' })
  })

  it('快捷键与右键菜单共用同一套可用性判断', () => {
    // 否则会出现"菜单里灰着、但快捷键能触发"
    const model = buildMenuModel(menuCtx({ selectedCount: 3 }))
    const viaShortcut = resolveShortcut(key('F2'), ctx({ model }))
    const viaMenu = model.find((e) => e.action === 'rename')
    expect(viaShortcut.kind).toBe(viaMenu?.disabledReason ? 'blocked' : 'action')
  })
})

describe('shouldPreventDefault', () => {
  it('action / blocked / escape 都要拦截', () => {
    // blocked 也必须拦截：F5 不拦会把整页重载掉，用户看到应用重新加载
    expect(shouldPreventDefault({ kind: 'action', action: 'delete' })).toBe(true)
    expect(shouldPreventDefault({ kind: 'blocked', action: 'refresh' })).toBe(true)
    expect(shouldPreventDefault({ kind: 'escape' })).toBe(true)
  })

  it('none 不拦截（避免吞掉正常输入）', () => {
    expect(shouldPreventDefault({ kind: 'none' })).toBe(false)
  })
})
