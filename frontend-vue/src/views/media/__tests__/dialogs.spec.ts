import { describe, expect, it } from 'vitest'

import { uniqueName } from '../clipboard'
import { buildConflictDialog, buildDeleteConfirm, type DeleteScopeDetail } from '../dialogs'

/**
 * 确认文案的用例。
 *
 * 文案是"用户是否会停下来看"的唯一变量：只写"确定删除吗？"时人会条件反射点确定。
 * 所以三种形态（一句话 / 精确代价 / 代价未知）必须严格区分，尤其**不许编造数字**。
 */

const scope = (override: Partial<DeleteScopeDetail> = {}): DeleteScopeDetail => ({
  selectedCount: 1,
  folderCount: 0,
  fileCount: 1,
  descendantCount: null,
  ...override,
})

describe('buildDeleteConfirm（删除确认文案）', () => {
  it('少量纯文件 → plain，一句话带数量', () => {
    const text = buildDeleteConfirm(scope({ selectedCount: 3, fileCount: 3 }))
    expect(text.danger).toBe('plain')
    expect(text.messageKey).toBe('media.delete_confirm_plain')
    expect(text.params).toEqual({ n: 3 })
  })

  it('恰好 5 个仍是 plain（边界）', () => {
    expect(buildDeleteConfirm(scope({ selectedCount: 5, fileCount: 5 })).danger).toBe('plain')
  })

  it('超过 5 个文件 → strong', () => {
    expect(buildDeleteConfirm(scope({ selectedCount: 6, fileCount: 6 })).danger).toBe('strong')
  })

  it('含文件夹 → strong，且给精确数字', () => {
    const text = buildDeleteConfirm(scope({
      selectedCount: 2, folderCount: 1, fileCount: 1, descendantCount: 10,
    }))
    expect(text.danger).toBe('strong')
    expect(text.messageKey).toBe('media.delete_confirm_strong')
    expect(text.params).toEqual({ folders: 1, files: 1 })
  })

  it('含文件夹但递归数未知 → 明确说"含全部子项"，不编造数字', () => {
    // 编一个数字（比如拿 selectedCount）会低报代价：用户以为删 2 项，实际删 47 个
    const text = buildDeleteConfirm(scope({
      selectedCount: 2, folderCount: 1, fileCount: 1, descendantCount: null,
    }))
    expect(text.messageKey).toBe('media.delete_confirm_strong_unknown')
    expect(text.params).toEqual({})
  })

  it('递归项数 >= 50 时要求主动勾选确认', () => {
    const text = buildDeleteConfirm(scope({
      selectedCount: 1, folderCount: 1, fileCount: 0, descendantCount: 50,
    }))
    expect(text.requireAcknowledge).toBe(true)
  })

  it('递归项数 < 50 时不要求勾选（避免每次删文件夹都要多一步）', () => {
    const text = buildDeleteConfirm(scope({
      selectedCount: 1, folderCount: 1, fileCount: 0, descendantCount: 49,
    }))
    expect(text.requireAcknowledge).toBe(false)
  })

  it('纯文件即使数量多也不要求勾选（数量已写在文案里）', () => {
    const text = buildDeleteConfirm(scope({ selectedCount: 20, fileCount: 20 }))
    expect(text.requireAcknowledge).toBe(false)
  })

  it('标题固定为确认删除', () => {
    expect(buildDeleteConfirm(scope()).titleKey).toBe('media.delete_confirm_title')
  })
})

describe('buildConflictDialog（冲突对话框）', () => {
  const conflicts = [
    { id: '1', name: 'a.txt' },
    { id: '2', name: 'b.txt' },
  ]

  it('行数等于冲突数，参数带 n', () => {
    const dlg = buildConflictDialog(conflicts, new Map(), new Set(['a.txt']), uniqueName)
    expect(dlg.rows).toHaveLength(2)
    expect(dlg.params).toEqual({ n: 2 })
  })

  it('未指定处置时默认跳过（不静默覆盖）', () => {
    const dlg = buildConflictDialog(conflicts, new Map(), new Set(), uniqueName)
    expect(dlg.rows.every((r) => r.choice === 'skip')).toBe(true)
  })

  it('keep-both 给出预览名，用户确认前就能看到结果', () => {
    // 不给预览的话，用户只能赌自己选完会变成什么
    const dlg = buildConflictDialog(
      conflicts, new Map([['1', 'keep-both']]), new Set(['a.txt']), uniqueName,
    )
    expect(dlg.rows[0]?.previewName).toBe('a (2).txt')
  })

  it('同批多个 keep-both 互相避让（不会两个都叫 a (2).txt）', () => {
    const dup = [
      { id: '1', name: 'a.txt' },
      { id: '2', name: 'a.txt' },
    ]
    const choices = new Map([
      ['1', 'keep-both' as const],
      ['2', 'keep-both' as const],
    ])
    const dlg = buildConflictDialog(dup, choices, new Set(['a.txt']), uniqueName)
    expect(dlg.rows.map((r) => r.previewName)).toEqual(['a (2).txt', 'a (3).txt'])
  })

  it('replace / skip 的预览名保持原名（不凭空加序号）', () => {
    const dlg = buildConflictDialog(
      conflicts,
      new Map([['1', 'replace'], ['2', 'skip']]),
      new Set(['a.txt', 'b.txt']),
      uniqueName,
    )
    expect(dlg.rows[0]?.previewName).toBe('a.txt')
    expect(dlg.rows[1]?.previewName).toBe('b.txt')
  })

  it('只有一个冲突时不显示"对全部应用"（冗余噪音）', () => {
    expect(buildConflictDialog([conflicts[0]!], new Map(), new Set(), uniqueName).showApplyAll)
      .toBe(false)
  })

  it('多个冲突时显示"对全部应用"（否则要一个个点）', () => {
    expect(buildConflictDialog(conflicts, new Map(), new Set(), uniqueName).showApplyAll).toBe(true)
  })

  it('标题与消息键指向 media 命名空间', () => {
    const dlg = buildConflictDialog(conflicts, new Map(), new Set(), uniqueName)
    expect(dlg.titleKey).toBe('media.conflict_title')
    expect(dlg.messageKey).toBe('media.conflict_message')
  })
})
