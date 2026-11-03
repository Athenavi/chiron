import { describe, expect, it } from 'vitest'

import {
  buildMenuDescriptors,
  buildMenuModel,
  LABEL_KEYS,
  MENU_GROUPS,
  SHORTCUTS,
  type MenuContext,
} from '../menuModel'

/**
 * 菜单**项列表**（分组、顺序、分割线、文案键、快捷键）的用例。
 *
 * 为什么测纯函数而不是挂载组件：antd 的 `Dropdown` 浮层是**懒渲染**的 ——
 * 右键之前 overlay 根本没有挂载，`findComponent(Menu)` 拿到空 wrapper。
 * 这类"渲染不出来所以测不了"的结构，正确做法是把可判定的部分抽出来，
 * 而不是在测试里手动触发展开再断言（那样测的是 antd，不是我们的逻辑）。
 *
 * 求值层（哪些项可用）在 `menuModel.spec.ts`，本文件只钉"渲染合同"。
 */

const ctx = (override: Partial<MenuContext> = {}): MenuContext => ({
  selectedCount: 0,
  hasFolder: false,
  hasFile: false,
  clipboard: { count: 0, mode: null },
  copySupported: true,
  moveSupported: true,
  ...override,
})

const descs = (override: Partial<MenuContext> = {}) =>
  buildMenuDescriptors(buildMenuModel(ctx(override)))

const actionsOf = (override: Partial<MenuContext> = {}) =>
  descs(override).map((d) => d.action)

describe('buildMenuDescriptors（渲染项列表）', () => {
  it('未选中时只出现与选择无关的项', () => {
    expect(actionsOf()).toEqual([
      'paste', 'refresh', 'new-folder', 'upload', 'select-all',
    ])
  })

  it('选中一项时出现单项操作', () => {
    const actions = actionsOf({ selectedCount: 1, hasFile: true })
    for (const action of ['open', 'rename', 'properties', 'delete', 'copy', 'cut'] as const) {
      expect(actions).toContain(action)
    }
  })

  it('顺序固定为 MENU_GROUPS 的声明顺序（不按可用性重排）', () => {
    // 菜单项位置随选择跳动时，肌肉记忆完全失效
    const actions = actionsOf({ selectedCount: 1, hasFile: true })
    const flatGroups = MENU_GROUPS.flat().filter((a) => actions.includes(a))
    expect(actions).toEqual(flatGroups)
  })

  it('不可见项整条剔除，不产生占位', () => {
    // 空白处右键看到一堆灰掉的"打开/重命名"，会让人以为功能坏了
    expect(actionsOf()).not.toContain('open')
    expect(actionsOf()).not.toContain('rename')
  })

  it('第一个项不带分割线', () => {
    expect(descs()[0]?.dividerBefore).toBe(false)
  })

  it('分割线只出现在组的第一项上', () => {
    const list = descs({ selectedCount: 1, hasFile: true })
    const withDivider = list.filter((d) => d.dividerBefore)
    // 有 N 组可见就有 N-1 条分割线
    const groups = MENU_GROUPS.filter((g) => g.some((a) => list.some((d) => d.action === a)))
    expect(withDivider).toHaveLength(Math.max(0, groups.length - 1))
  })

  it('不产生连续分割线，也不在末尾留分割线', () => {
    const list = descs({ selectedCount: 1, hasFile: true })
    if (list.length > 0) expect(list[0]?.dividerBefore).toBe(false)
    for (let i = 1; i < list.length; i += 1) {
      // 每一项都可以带 dividerBefore（它是"我前面有线"），
      // 但两组之间只能有一条，由"仅组首项标记"保证
      expect(list[i]?.dividerBefore).toBe(
        !MENU_GROUPS.some((g) => g.includes(list[i]!.action) && g.includes(list[i - 1]!.action)),
      )
    }
  })

  it('每项都有文案键，禁用项额外带原因键', () => {
    const list = descs({ selectedCount: 3, hasFile: true })
    for (const d of list) {
      expect(d.labelKey).toBe(LABEL_KEYS[d.action])
      if (d.disabled) expect(d.reasonKey).not.toBe('')
      else expect(d.reasonKey).toBe('')
    }
  })

  it('复制/剪切/粘贴带快捷键提示（右键菜单是发现快捷键的主要途径）', () => {
    const list = descs({ selectedCount: 1, hasFile: true, clipboard: { count: 1, mode: 'copy' } })
    const find = (a: string) => list.find((d) => d.action === a)
    expect(find('copy')?.shortcut).toBe('Ctrl+C')
    expect(find('cut')?.shortcut).toBe('Ctrl+X')
    expect(find('paste')?.shortcut).toBe('Ctrl+V')
  })

  it('没有登记快捷键的项 shortcut 为空串（不是 undefined）', () => {
    const list = descs()
    const refresh = list.find((d) => d.action === 'refresh')
    expect(refresh?.shortcut).toBe('F5')
    const upload = list.find((d) => d.action === 'upload')
    expect(upload?.shortcut).toBe('')
  })

  it('所有 LABEL_KEYS 指向真实的 i18n 命名空间', () => {
    for (const key of Object.values(LABEL_KEYS)) {
      expect(key).toMatch(/^(media|common)\./)
    }
  })

  it('SHORTCUTS 的键都是合法动作名', () => {
    const valid = new Set(MENU_GROUPS.flat())
    for (const action of Object.keys(SHORTCUTS)) {
      expect(valid.has(action as never)).toBe(true)
    }
  })
})
