import { describe, expect, it } from 'vitest'

import {
  clipboardStorageKey,
  detectConflicts,
  EMPTY_CLIPBOARD,
  parseClipboard,
  pasteBlockedReason,
  planPaste,
  pruneClipboard,
  serializeClipboard,
  splitExt,
  uniqueName,
  type ClipboardPayload,
} from '../clipboard'

/**
 * 剪贴板与粘贴冲突的用例。
 *
 * 这些边界一旦错，后果是**数据被覆盖**或**名字撞在一起**，两者都不会抛异常：
 * 前者用户过几天才发现原文件没了，后者媒体库里出现两个同名项。
 */

const payload = (override: Partial<ClipboardPayload> = {}): ClipboardPayload => ({
  mode: 'copy',
  ids: ['a', 'b'],
  fromParentId: 'root',
  ...override,
})

describe('clipboardStorageKey（按租户+用户隔离）', () => {
  it('包含租户与用户', () => {
    expect(clipboardStorageKey('1', 'u1')).toBe('chiron.media.clipboard:1:u1')
  })

  it('不同用户拿到不同键（防止跨账号看到别人的资产 id）', () => {
    expect(clipboardStorageKey('1', 'u1')).not.toBe(clipboardStorageKey('1', 'u2'))
  })

  it('空值有稳定占位，不产生尾部悬空分隔符', () => {
    expect(clipboardStorageKey('', '')).toBe('chiron.media.clipboard:-:-')
  })
})

describe('serialize / parse（防御式反序列化）', () => {
  it('往返一致', () => {
    const p = payload({ mode: 'cut', ids: ['x'] })
    expect(parseClipboard(serializeClipboard(p))).toEqual(p)
  })

  it('null / 空串 / 非法 JSON → 空剪贴板', () => {
    expect(parseClipboard(null)).toEqual(EMPTY_CLIPBOARD)
    expect(parseClipboard('')).toEqual(EMPTY_CLIPBOARD)
    expect(parseClipboard('{not json')).toEqual(EMPTY_CLIPBOARD)
  })

  it('非对象 → 空剪贴板', () => {
    expect(parseClipboard('"str"')).toEqual(EMPTY_CLIPBOARD)
    expect(parseClipboard('123')).toEqual(EMPTY_CLIPBOARD)
    expect(parseClipboard('null')).toEqual(EMPTY_CLIPBOARD)
  })

  it('mode 非法 → 空剪贴板（不猜成 copy）', () => {
    // 猜错 mode 的后果是把"剪切"当"复制"执行，源文件没被移走，用户以为移动完成了
    expect(parseClipboard(JSON.stringify({ mode: 'move', ids: ['a'], fromParentId: '' })))
      .toEqual(EMPTY_CLIPBOARD)
  })

  it('ids 为空数组 → 空剪贴板', () => {
    expect(parseClipboard(JSON.stringify({ mode: 'copy', ids: [], fromParentId: '' })))
      .toEqual(EMPTY_CLIPBOARD)
  })

  it('ids 里的非字符串项被剔除，其余保留', () => {
    const parsed = parseClipboard(
      JSON.stringify({ mode: 'copy', ids: ['a', 1, null, '', 'b'], fromParentId: '' }),
    )
    expect(parsed.ids).toEqual(['a', 'b'])
  })

  it('缺 fromParentId → 空剪贴板（半份剪贴板比没有更危险）', () => {
    expect(parseClipboard(JSON.stringify({ mode: 'copy', ids: ['a'] }))).toEqual(EMPTY_CLIPBOARD)
  })
})

describe('pruneClipboard（剔除已删除的源项）', () => {
  it('全部存活时返回原对象', () => {
    const p = payload()
    expect(pruneClipboard(p, new Set(['a', 'b']))).toBe(p)
  })

  it('剔除已删除的 id', () => {
    const p = pruneClipboard(payload(), new Set(['a']))
    expect(p.ids).toEqual(['a'])
  })

  it('全部消失 → 空剪贴板（粘贴项应随之置灰）', () => {
    // 不置灰的话，用户点了再收到"源项不存在"，与当前操作毫无关系
    expect(pruneClipboard(payload(), new Set())).toEqual(EMPTY_CLIPBOARD)
  })
})

describe('splitExt / uniqueName（Windows 式"保留两者"）', () => {
  it('拆分扩展名', () => {
    expect(splitExt('report.txt')).toEqual({ stem: 'report', ext: '.txt' })
    expect(splitExt('notes')).toEqual({ stem: 'notes', ext: '' })
  })

  it('点开头的名字不当作扩展名', () => {
    // `.gitignore` 是隐藏文件名；`(.gitignore)` 那种结果资源管理器里也不会出现
    expect(splitExt('.gitignore')).toEqual({ stem: '.gitignore', ext: '' })
  })

  it('尾随点不当作扩展名', () => {
    expect(splitExt('weird.')).toEqual({ stem: 'weird.', ext: '' })
  })

  it('不冲突时原样返回', () => {
    expect(uniqueName('report.txt', new Set())).toBe('report.txt')
  })

  it('冲突时从 (2) 开始', () => {
    expect(uniqueName('report.txt', new Set(['report.txt']))).toBe('report (2).txt')
  })

  it('连号递增到第一个空位', () => {
    const taken = new Set(['report.txt', 'report (2).txt', 'report (3).txt'])
    expect(uniqueName('report.txt', taken)).toBe('report (4).txt')
  })

  it('无扩展名的文件把序号加在末尾', () => {
    expect(uniqueName('notes', new Set(['notes']))).toBe('notes (2)')
  })

  it('点开头的文件同样把序号加在末尾', () => {
    expect(uniqueName('.gitignore', new Set(['.gitignore']))).toBe('.gitignore (2)')
  })

  it('序号插在扩展名之前（不是追加到最后）', () => {
    // `report.txt (2)` 会让系统认不出扩展名
    const got = uniqueName('report.txt', new Set(['report.txt']))
    expect(got.endsWith('.txt')).toBe(true)
  })
})

describe('detectConflicts（按名字判冲突）', () => {
  const sources = [
    { id: '1', name: 'a.txt' },
    { id: '2', name: 'b.txt' },
  ]

  it('只有同名项算冲突', () => {
    expect(detectConflicts(sources, new Set(['b.txt']))).toEqual([{ id: '2', name: 'b.txt' }])
  })

  it('无冲突返回空数组', () => {
    expect(detectConflicts(sources, new Set(['c.txt']))).toEqual([])
  })

  it('空源列表', () => {
    expect(detectConflicts([], new Set(['a.txt']))).toEqual([])
  })
})

describe('planPaste（粘贴执行计划）', () => {
  const sources = [
    { id: '1', name: 'a.txt' },
    { id: '2', name: 'b.txt' },
  ]

  it('无冲突时全部按原名创建', () => {
    const plan = planPaste(sources, new Set(), new Map())
    expect(plan).toEqual([
      { id: '1', sourceName: 'a.txt', choice: 'replace', finalName: 'a.txt' },
      { id: '2', sourceName: 'b.txt', choice: 'replace', finalName: 'b.txt' },
    ])
  })

  it('冲突项默认跳过（不静默覆盖）', () => {
    // 没有明确选择时**不覆盖**：静默覆盖是数据丢失，宁可少做一个动作
    const plan = planPaste(sources, new Set(['a.txt']), new Map())
    expect(plan.map((p) => p.id)).toEqual(['2'])
  })

  it('选择替换时用原名', () => {
    const plan = planPaste(sources, new Set(['a.txt']), new Map([['1', 'replace']]))
    expect(plan).toContainEqual({
      id: '1', sourceName: 'a.txt', choice: 'replace', finalName: 'a.txt',
    })
  })

  it('选择保留两者时生成新名字', () => {
    const plan = planPaste(sources, new Set(['a.txt']), new Map([['1', 'keep-both']]))
    expect(plan).toContainEqual({
      id: '1', sourceName: 'a.txt', choice: 'keep-both', finalName: 'a (2).txt',
    })
  })

  it('多个同名项**在一次计划内互相避让**（否则两份都叫 a (2).txt）', () => {
    const dup = [
      { id: '1', name: 'a.txt' },
      { id: '2', name: 'a.txt' },
    ]
    const choices = new Map([
      ['1', 'keep-both' as const],
      ['2', 'keep-both' as const],
    ])
    const plan = planPaste(dup, new Set(['a.txt']), choices)
    expect(plan.map((p) => p.finalName)).toEqual(['a (2).txt', 'a (3).txt'])
  })

  it('跳过项不进计划（调用方能据此报告"实际做了几项"）', () => {
    const plan = planPaste(sources, new Set(['a.txt', 'b.txt']), new Map())
    expect(plan).toEqual([])
  })
})

describe('pasteBlockedReason（无效粘贴的前置判断）', () => {
  it('空剪贴板', () => {
    expect(pasteBlockedReason(EMPTY_CLIPBOARD, 'root')).toBe('empty')
  })

  it('剪切到原目录 → 无效', () => {
    // 资源管理器在这种情况下静默不做事；这里显式给出原因好在菜单里区分
    expect(pasteBlockedReason(payload({ mode: 'cut', fromParentId: 'root' }), 'root'))
      .toBe('same-directory')
  })

  it('复制到原目录是合法的（会生成副本）', () => {
    expect(pasteBlockedReason(payload({ mode: 'copy', fromParentId: 'root' }), 'root')).toBeNull()
  })

  it('剪切到别的目录是合法的', () => {
    expect(pasteBlockedReason(payload({ mode: 'cut', fromParentId: 'root' }), 'other')).toBeNull()
  })
})
