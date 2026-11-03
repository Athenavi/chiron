import { describe, expect, it } from 'vitest'

import {
  buildMenuModel,
  canRun,
  deleteDanger,
  entryOf,
  REASON,
  type MenuContext,
} from '../menuModel'

/**
 * 菜单求值层的用例。
 *
 * 这些规则容易被写成"看起来对"的样子：`>= 1` 写成 `> 1`、把"没选东西"和
 * "后端不支持"混成一句原因 —— 而它们在界面上的表现只是"某项偶尔点不动"，
 * 用户不会报 bug，只会觉得这个功能不好用。所以条件必须钉在这里。
 */

const baseCtx = (override: Partial<MenuContext> = {}): MenuContext => ({
  selectedCount: 0,
  hasFolder: false,
  hasFile: false,
  clipboard: { count: 0, mode: null },
  copySupported: true,
  moveSupported: true,
  ...override,
})

/** 取某动作的原因，便于断言。 */
const reasonOf = (ctx: MenuContext, action: Parameters<typeof entryOf>[1]) =>
  entryOf(buildMenuModel(ctx), action)?.disabledReason

describe('buildMenuModel（菜单可用性）', () => {
  it('未选中任何项：打开/复制/剪切/重命名/删除/属性 均不可用且给出"请先选择"', () => {
    const ctx = baseCtx()
    for (const action of ['open', 'copy', 'cut', 'rename', 'delete', 'properties'] as const) {
      expect(reasonOf(ctx, action)).toBe(REASON.needSelection)
    }
  })

  it('未选中时这些项**不可见**（而非仅禁用）', () => {
    // 空白处右键不该看到一堆灰掉的"打开/重命名/属性" —— 那会让人以为功能坏了
    const model = buildMenuModel(baseCtx())
    for (const action of ['open', 'copy', 'cut', 'rename', 'delete', 'properties'] as const) {
      expect(entryOf(model, action)?.visible).toBe(false)
    }
  })

  it('未选中时刷新/新建文件夹/上传/全选仍然可用（空白处右键的核心用途）', () => {
    const model = buildMenuModel(baseCtx())
    for (const action of ['refresh', 'new-folder', 'upload', 'select-all'] as const) {
      expect(canRun(model, action)).toBe(true)
    }
  })

  it('选中一项：打开/重命名/属性可用', () => {
    const ctx = baseCtx({ selectedCount: 1, hasFile: true })
    for (const action of ['open', 'rename', 'properties'] as const) {
      expect(canRun(buildMenuModel(ctx), action)).toBe(true)
    }
  })

  it('选中多项：打开/重命名/属性不可用，且原因是"只能选一项"', () => {
    // 与"请先选择"区分开：这里选是选了的，只是不能作用于多个
    const ctx = baseCtx({ selectedCount: 3, hasFile: true })
    for (const action of ['open', 'rename', 'properties'] as const) {
      expect(reasonOf(ctx, action)).toBe(REASON.needOne)
    }
  })

  it('复制/剪切对多项**仍然可用**（Windows 支持批量复制）', () => {
    const ctx = baseCtx({ selectedCount: 3, hasFile: true })
    expect(canRun(buildMenuModel(ctx), 'copy')).toBe(true)
    expect(canRun(buildMenuModel(ctx), 'cut')).toBe(true)
  })

  it('混合选择（文件+文件夹）不阻止复制/剪切', () => {
    const ctx = baseCtx({ selectedCount: 2, hasFile: true, hasFolder: true })
    expect(canRun(buildMenuModel(ctx), 'copy')).toBe(true)
  })

  it('删除对多项可用（含文件夹）', () => {
    const ctx = baseCtx({ selectedCount: 4, hasFolder: true })
    expect(canRun(buildMenuModel(ctx), 'delete')).toBe(true)
  })

  it('粘贴：剪贴板为空时不可用并说明原因', () => {
    expect(reasonOf(baseCtx(), 'paste')).toBe(REASON.clipboardEmpty)
  })

  it('粘贴：剪贴板有内容时可用（且即使没有选中项也可用）', () => {
    // 空白处右键粘贴是资源管理器的核心操作，不能要求"先随便选一个"
    const ctx = baseCtx({ clipboard: { count: 2, mode: 'copy' } })
    expect(canRun(buildMenuModel(ctx), 'paste')).toBe(true)
  })

  it('粘贴项**始终可见**（即使剪贴板空也显示为禁用）', () => {
    expect(entryOf(buildMenuModel(baseCtx()), 'paste')?.visible).toBe(true)
  })

  it('后端不支持复制时：复制与复制态粘贴不可用，原因是"暂不支持"而不是"请先选择"', () => {
    // 两种不可用的原因必须分开：混成一句，用户会把"后端没实现"误认为"我操作错了"
    const ctx = baseCtx({
      selectedCount: 1,
      hasFile: true,
      clipboard: { count: 1, mode: 'copy' },
      copySupported: false,
    })
    for (const action of ['copy', 'paste'] as const) {
      expect(reasonOf(ctx, action)).toBe(REASON.copyUnavailable)
    }
  })

  it('不支持复制**不影响**剪切：剪切的落地是移动，走另一个能力开关', () => {
    // 把 copy/cut/paste 绑在同一个开关上，会让本来能用的剪切一起失效 ——
    // 那是把一个缺失放大成两个
    const ctx = baseCtx({ selectedCount: 1, hasFile: true, copySupported: false })
    expect(canRun(buildMenuModel(ctx), 'cut')).toBe(true)
  })

  it('剪切态粘贴只依赖 moveSupported', () => {
    const ctx = baseCtx({
      selectedCount: 1,
      hasFile: true,
      clipboard: { count: 1, mode: 'cut' },
      copySupported: false,
      moveSupported: true,
    })
    expect(canRun(buildMenuModel(ctx), 'paste')).toBe(true)
  })

  it('不支持移动时：剪切与其粘贴不可用，原因指向"移动"', () => {
    const ctx = baseCtx({
      selectedCount: 1,
      hasFile: true,
      clipboard: { count: 1, mode: 'cut' },
      moveSupported: false,
    })
    expect(reasonOf(ctx, 'cut')).toBe(REASON.moveUnavailable)
    // 剪切态粘贴缺的是"移动"能力，因此原因也应指向移动而不是含糊的"暂不支持粘贴" ——
    // 用户据此知道该等哪个能力，而不是换个目录瞎试
    expect(reasonOf(ctx, 'paste')).toBe(REASON.moveUnavailable)
  })

  it('不支持复制时**不影响**删除/重命名等本地操作', () => {
    const ctx = baseCtx({ selectedCount: 1, hasFile: true, copySupported: false })
    expect(canRun(buildMenuModel(ctx), 'delete')).toBe(true)
    expect(canRun(buildMenuModel(ctx), 'rename')).toBe(true)
  })
})

describe('entryOf / canRun', () => {
  it('未知动作返回 undefined', () => {
    // @ts-expect-error 故意传非法动作，验证边界不抛错
    expect(entryOf(buildMenuModel(baseCtx()), 'nope')).toBeUndefined()
  })

  it('不可见项即使用户误触发也不可执行', () => {
    const model = buildMenuModel(baseCtx())
    expect(canRun(model, 'open')).toBe(false)
  })
})

describe('deleteDanger（删除确认分级）', () => {
  it('少量文件 → 普通确认', () => {
    expect(deleteDanger({ selectedCount: 1, hasFolder: false, descendantCount: null })).toBe('plain')
  })

  it('超过 5 个文件 → 强确认', () => {
    expect(deleteDanger({ selectedCount: 6, hasFolder: false, descendantCount: null })).toBe('strong')
  })

  it('恰好 5 个仍是普通确认（边界）', () => {
    expect(deleteDanger({ selectedCount: 5, hasFolder: false, descendantCount: null })).toBe('plain')
  })

  it('含文件夹即强确认（哪怕只有一个）', () => {
    // 删空文件夹与删一棵树在心理上完全不同，而对话框长得一样时人会条件反射点确定
    expect(deleteDanger({ selectedCount: 1, hasFolder: true, descendantCount: 1 })).toBe('strong')
  })
})
