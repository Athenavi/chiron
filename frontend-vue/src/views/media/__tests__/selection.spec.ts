import { describe, expect, it } from 'vitest'

import {
  clearSelection,
  clickSelect,
  containsRect,
  EMPTY_SELECTION,
  invertSelection,
  marqueeSelect,
  modifierOf,
  normalizeRect,
  onlyFolders,
  pruneSelection,
  selectAll,
  selectedItems,
  type ItemRect,
  type Selectable,
  type SelectionState,
} from '../selection'

/**
 * 选择状态机的边界用例。
 *
 * 这些用例存在的理由很具体：框选与连选的错误**不会抛异常**，只会安静地选错一片。
 * 而"选错一片"如果出现在批量删除前，代价是数据丢失 —— 所以宁可把边界写死在这里。
 */

const items: Selectable[] = [
  { id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }, { id: 'e' },
]

const stateOf = (ids: string[], anchorId: string | null = null): SelectionState => ({
  selected: new Set(ids),
  anchorId,
})

const idsOf = (s: SelectionState): string[] => [...s.selected].sort()

describe('modifierOf（修饰键归一化）', () => {
  it('识别 ctrl / shift / 组合', () => {
    expect(modifierOf({})).toBe('none')
    expect(modifierOf({ ctrlKey: true })).toBe('ctrl')
    expect(modifierOf({ shiftKey: true })).toBe('shift')
    expect(modifierOf({ ctrlKey: true, shiftKey: true })).toBe('shift-ctrl')
  })

  it('把 metaKey 当作 ctrl（mac 的 Command）', () => {
    // 只认 ctrlKey 会让 mac 上的多选完全失效，而用户不会来报 bug，只会觉得"不好用"
    expect(modifierOf({ metaKey: true })).toBe('ctrl')
    expect(modifierOf({ metaKey: true, shiftKey: true })).toBe('shift-ctrl')
  })
})

describe('clickSelect（单击 / Ctrl / Shift）', () => {
  it('普通单击：只选这一项，锚点移到它', () => {
    const s = clickSelect(items, stateOf(['a', 'b'], 'a'), 'c', 'none')
    expect(idsOf(s)).toEqual(['c'])
    expect(s.anchorId).toBe('c')
  })

  it('Ctrl 单击：追加并移动锚点', () => {
    const s = clickSelect(items, stateOf(['a'], 'a'), 'c', 'ctrl')
    expect(idsOf(s)).toEqual(['a', 'c'])
    expect(s.anchorId).toBe('c')
  })

  it('Ctrl 单击已选中项：从选择中移除，锚点仍移到它', () => {
    // Windows 行为：取消选中后，紧接着 Shift 会以它为基准
    const s = clickSelect(items, stateOf(['a', 'c'], 'a'), 'c', 'ctrl')
    expect(idsOf(s)).toEqual(['a'])
    expect(s.anchorId).toBe('c')
  })

  it('Shift 单击：选中 anchor→target 闭区间（正向）', () => {
    const s = clickSelect(items, stateOf(['b'], 'b'), 'd', 'shift')
    expect(idsOf(s)).toEqual(['b', 'c', 'd'])
  })

  it('Shift 单击：反向拖也正确（target 在 anchor 之前）', () => {
    const s = clickSelect(items, stateOf(['d'], 'd'), 'b', 'shift')
    expect(idsOf(s)).toEqual(['b', 'c', 'd'])
    // 反向时锚点不动 —— 否则连按两次 Shift 会滚雪球
    expect(s.anchorId).toBe('d')
  })

  it('Shift 连选不改锚点（连按两次不滚雪球）', () => {
    const first = clickSelect(items, stateOf(['b'], 'b'), 'c', 'shift')
    const second = clickSelect(items, first, 'e', 'shift')
    expect(idsOf(second)).toEqual(['b', 'c', 'd', 'e'])
    expect(second.anchorId).toBe('b')
  })

  it('Shift 连选替换现有选择（不是追加）', () => {
    const s = clickSelect(items, stateOf(['a'], 'c'), 'e', 'shift')
    expect(idsOf(s)).toEqual(['c', 'd', 'e'])
  })

  it('Shift+Ctrl：把区间追加进现有选择', () => {
    const s = clickSelect(items, stateOf(['a'], 'c'), 'd', 'shift-ctrl')
    expect(idsOf(s)).toEqual(['a', 'c', 'd'])
  })

  it('没有锚点时 Shift 退化为普通单击', () => {
    const s = clickSelect(items, EMPTY_SELECTION, 'c', 'shift')
    expect(idsOf(s)).toEqual(['c'])
    expect(s.anchorId).toBe('c')
  })

  it('锚点项已不在列表里时 Shift 退化为普通单击', () => {
    // 刷新/翻页后锚点可能已消失；此时"从基准拉到这里"没有定义
    const s = clickSelect(items, stateOf(['zzz'], 'zzz'), 'c', 'shift')
    expect(idsOf(s)).toEqual(['c'])
  })

  it('没有锚点时 Shift+Ctrl 只追加目标项', () => {
    const s = clickSelect(items, stateOf(['a']), 'c', 'shift-ctrl')
    expect(idsOf(s)).toEqual(['a', 'c'])
  })

  it('区间按 items 顺序算，不是 DOM 顺序', () => {
    // 关键在"中间隔着谁"：同一批项在不同排序下，anchor→target 之间夹的东西不同。
    // 若按 DOM 顺序（a,b,c）算，a→b 只选 {a,b}；按 items 顺序（a,c,b）算，要选 {a,c,b}。
    const reordered: Selectable[] = [{ id: 'a' }, { id: 'c' }, { id: 'b' }]
    const s = clickSelect(reordered, stateOf(['a'], 'a'), 'b', 'shift')
    expect(idsOf(s)).toEqual(['a', 'b', 'c'])
  })

  it('反转后的列表同样按它自己的顺序算区间', () => {
    const reversed: Selectable[] = [
      { id: 'e' }, { id: 'd' }, { id: 'c' }, { id: 'b' }, { id: 'a' },
    ]
    // anchor=b（索引3）到 target=d（索引1），区间是 d,c,b
    const s = clickSelect(reversed, stateOf(['b'], 'b'), 'd', 'shift')
    expect(idsOf(s)).toEqual(['b', 'c', 'd'])
  })

  it('永远返回新的 Set（Vue 才能感知变化）', () => {
    const before = stateOf(['a'], 'a')
    const after = clickSelect(items, before, 'a', 'none')
    expect(after.selected).not.toBe(before.selected)
  })
})

describe('normalizeRect（拖拽矩形归一化）', () => {
  it('从左上拖到右下', () => {
    expect(normalizeRect({ x: 10, y: 20 }, { x: 50, y: 80 })).toEqual({
      left: 10, top: 20, right: 50, bottom: 80,
    })
  })

  it('从右下拖到左上（必须归一化）', () => {
    // 不归一化的话 right < left，"完全落入"判断永远为假 ——
    // 表现为"往一个方向拖能选、反方向拖选不中"
    expect(normalizeRect({ x: 50, y: 80 }, { x: 10, y: 20 })).toEqual({
      left: 10, top: 20, right: 50, bottom: 80,
    })
  })
})

describe('containsRect（完全落入）', () => {
  const outer = { left: 0, top: 0, right: 100, bottom: 100 }

  it('完全在内 → true', () => {
    expect(containsRect(outer, { left: 10, top: 10, right: 20, bottom: 20 })).toBe(true)
  })

  it('边界相接 → true', () => {
    expect(containsRect(outer, { left: 0, top: 0, right: 100, bottom: 100 })).toBe(true)
  })

  it('只部分相交 → false', () => {
    // 与 Windows 一致：擦过边不算选中
    expect(containsRect(outer, { left: 50, top: 50, right: 150, bottom: 150 })).toBe(false)
  })

  it('完全在外 → false', () => {
    expect(containsRect(outer, { left: 200, top: 200, right: 300, bottom: 300 })).toBe(false)
  })
})

describe('marqueeSelect（框选）', () => {
  const rects: ItemRect[] = [
    { id: 'a', left: 0, top: 0, right: 40, bottom: 40 },
    { id: 'b', left: 50, top: 0, right: 90, bottom: 40 },
    { id: 'c', left: 0, top: 50, right: 40, bottom: 90 },
  ]

  it('只命中完全落入矩形的项', () => {
    const s = marqueeSelect(rects, { left: -5, top: -5, right: 45, bottom: 45 }, EMPTY_SELECTION)
    expect(idsOf(s)).toEqual(['a'])
  })

  it('覆盖两项', () => {
    const s = marqueeSelect(
      rects, { left: -5, top: -5, right: 95, bottom: 45 }, EMPTY_SELECTION,
    )
    expect(idsOf(s)).toEqual(['a', 'b'])
  })

  it('部分相交不算命中', () => {
    const s = marqueeSelect(
      rects, { left: 20, top: -5, right: 60, bottom: 45 }, EMPTY_SELECTION,
    )
    expect(idsOf(s)).toEqual([])
  })

  it('非追加时替换现有选择', () => {
    const s = marqueeSelect(
      rects, { left: -5, top: -5, right: 95, bottom: 45 }, stateOf(['c'], 'c'),
    )
    expect(idsOf(s)).toEqual(['a', 'b'])
  })

  it('Ctrl 框选时并入现有选择', () => {
    const s = marqueeSelect(
      rects, { left: -5, top: -5, right: 45, bottom: 45 }, stateOf(['c'], 'c'), true,
    )
    expect(idsOf(s)).toEqual(['a', 'c'])
  })

  it('框选结果成为新锚点（之后 Shift 从此开始）', () => {
    const s = marqueeSelect(rects, { left: -5, top: -5, right: 95, bottom: 45 }, EMPTY_SELECTION)
    expect(s.anchorId).toBe('a')
  })

  it('空框选清空选择且锚点置空', () => {
    const s = marqueeSelect(rects, { left: 500, top: 500, right: 600, bottom: 600 }, stateOf(['a'], 'a'))
    expect(idsOf(s)).toEqual([])
    expect(s.anchorId).toBeNull()
  })
})

describe('selectAll / clearSelection / invertSelection', () => {
  it('全选', () => {
    const s = selectAll(items, EMPTY_SELECTION)
    expect(idsOf(s)).toEqual(['a', 'b', 'c', 'd', 'e'])
    // 补一个锚点，让全选后 Shift 仍可用
    expect(s.anchorId).toBe('a')
  })

  it('全选保留已有锚点', () => {
    expect(selectAll(items, stateOf(['c'], 'c')).anchorId).toBe('c')
  })

  it('清空', () => {
    const s = clearSelection()
    expect(idsOf(s)).toEqual([])
    expect(s.anchorId).toBeNull()
  })

  it('反选', () => {
    const s = invertSelection(items, stateOf(['a', 'd'], 'a'))
    expect(idsOf(s)).toEqual(['b', 'c', 'e'])
    expect(s.anchorId).toBe('a')
  })
})

describe('pruneSelection（剔除已删除的项）', () => {
  it('剔除不存在的 id', () => {
    const s = pruneSelection(items, stateOf(['a', 'zzz'], 'a'))
    expect(idsOf(s)).toEqual(['a'])
  })

  it('锚点项消失时清空锚点', () => {
    // 否则 Shift 连选会算出空区间，看起来"点不动"
    const s = pruneSelection(items, stateOf(['a'], 'zzz'))
    expect(s.anchorId).toBeNull()
  })

  it('无变化时返回原对象（避免下游 watch 空转）', () => {
    const before = stateOf(['a', 'b'], 'a')
    expect(pruneSelection(items, before)).toBe(before)
  })

  it('空选择且无锚点时返回原对象', () => {
    const before = EMPTY_SELECTION
    expect(pruneSelection(items, before)).toBe(before)
  })

  it('锚点为 null 时不会误判为"变化"', () => {
    const before = stateOf(['a'], null)
    expect(pruneSelection(items, before)).toBe(before)
  })
})

describe('selectedItems / onlyFolders', () => {
  const withTypes = [
    { id: 'f1', type: 'folder' },
    { id: 'x1', type: 'text' },
    { id: 'f2', type: 'folder' },
  ]

  it('按列表顺序返回选中的项（稳定执行次序）', () => {
    const picked = selectedItems(withTypes, stateOf(['f2', 'x1']))
    expect(picked.map((i) => i.id)).toEqual(['x1', 'f2'])
  })

  it('全是文件夹 → true', () => {
    expect(onlyFolders(withTypes, stateOf(['f1', 'f2']))).toBe(true)
  })

  it('混有文件 → false', () => {
    expect(onlyFolders(withTypes, stateOf(['f1', 'x1']))).toBe(false)
  })

  it('空选择 → false（没有"全是什么"可谈）', () => {
    expect(onlyFolders(withTypes, EMPTY_SELECTION)).toBe(false)
  })
})
