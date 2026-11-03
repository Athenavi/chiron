import { beforeEach, describe, expect, it } from 'vitest'

import { useMediaClipboard } from '../useMediaClipboard'
import { clipboardStorageKey, type ClipboardPayload } from '../../views/media/clipboard'

/**
 * 剪贴板 composable 的用例。
 *
 * 纯逻辑（序列化、剔除、冲突）在 `clipboard.spec.ts` 已穷举；这里只钉
 * **存储与账号隔离**这层 —— 它决定了"换账号后会不会看到别人的剪贴板"，
 * 而这个错误的后果不是功能不好用，是越权。
 */

const payload = (override: Partial<ClipboardPayload> = {}): ClipboardPayload => ({
  mode: 'copy',
  ids: ['a', 'b'],
  fromParentId: 'root',
  ...override,
})

describe('useMediaClipboard（存储与账号隔离）', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it('初始为空剪贴板', () => {
    const cb = useMediaClipboard()
    expect(cb.hasContent.value).toBe(false)
    expect(cb.isCutting.value).toBe(false)
  })

  it('写入后可读回，且落到 sessionStorage', () => {
    const cb = useMediaClipboard()
    cb.syncKey('1', 'u1')
    cb.write(payload())

    expect(cb.hasContent.value).toBe(true)
    expect(sessionStorage.getItem(clipboardStorageKey('1', 'u1'))).toBeTruthy()
  })

  it('刷新后（新建实例）剪贴板仍在', () => {
    // 与资源管理器一致：复制后切个标签页回来，不该发现剪贴板空了
    const first = useMediaClipboard()
    first.syncKey('1', 'u1')
    first.write(payload({ mode: 'cut' }))

    const second = useMediaClipboard()
    second.syncKey('1', 'u1')
    expect(second.payload.value.mode).toBe('cut')
    expect(second.payload.value.ids).toEqual(['a', 'b'])
  })

  it('不同账号读到不同剪贴板（结构性隔离，不依赖登出清理）', () => {
    const u1 = useMediaClipboard()
    u1.syncKey('1', 'u1')
    u1.write(payload({ ids: ['secret'] }))

    // 换账号：键不同 ⇒ 读不到旧数据
    const u2 = useMediaClipboard()
    u2.syncKey('1', 'u2')
    expect(u2.hasContent.value).toBe(false)
  })

  it('不同租户也隔离', () => {
    const t1 = useMediaClipboard()
    t1.syncKey('1', 'u1')
    t1.write(payload())

    const t2 = useMediaClipboard()
    t2.syncKey('2', 'u1')
    expect(t2.hasContent.value).toBe(false)
  })

  it('同一账号切回来仍能看到自己的剪贴板', () => {
    const a = useMediaClipboard()
    a.syncKey('1', 'u1')
    a.write(payload({ ids: ['mine'] }))

    const b = useMediaClipboard()
    b.syncKey('1', 'u2')
    // 切回 u1
    b.syncKey('1', 'u1')
    expect(b.payload.value.ids).toEqual(['mine'])
  })

  it('syncKey 相同账号时不重读（避免覆盖尚未落盘的改动）', () => {
    const cb = useMediaClipboard()
    cb.syncKey('1', 'u1')
    cb.write(payload())

    // 手工改掉存储里的内容，再 syncKey 一次（同账号）
    sessionStorage.setItem(
      clipboardStorageKey('1', 'u1'),
      JSON.stringify({ mode: 'copy', ids: ['hacked'], fromParentId: '' }),
    )
    cb.syncKey('1', 'u1')
    // 未变键 ⇒ 不重读，内存里那份仍是写入的
    expect(cb.payload.value.ids).toEqual(['a', 'b'])
  })

  it('清空同时清掉存储', () => {
    const cb = useMediaClipboard()
    cb.syncKey('1', 'u1')
    cb.write(payload())
    cb.clear()

    expect(cb.hasContent.value).toBe(false)
    expect(sessionStorage.getItem(clipboardStorageKey('1', 'u1'))).toBeNull()
  })

  it('cut 态标记正确（UI 据此虚化源项）', () => {
    const cb = useMediaClipboard()
    cb.syncKey('1', 'u1')
    cb.write(payload({ mode: 'cut' }))
    expect(cb.isCutting.value).toBe(true)

    cb.write(payload({ mode: 'copy' }))
    expect(cb.isCutting.value).toBe(false)
  })

  it('prune 剔除已删除的源项并回写存储', () => {
    const cb = useMediaClipboard()
    cb.syncKey('1', 'u1')
    cb.write(payload({ ids: ['a', 'b'] }))

    const pruned = cb.prune(new Set(['a']))
    expect(pruned.ids).toEqual(['a'])
    expect(cb.payload.value.ids).toEqual(['a'])

    const stored = JSON.parse(sessionStorage.getItem(clipboardStorageKey('1', 'u1')) || '{}')
    expect(stored.ids).toEqual(['a'])
  })

  it('prune 全部失效时剪贴板变空（粘贴项应随之置灰）', () => {
    const cb = useMediaClipboard()
    cb.syncKey('1', 'u1')
    cb.write(payload())

    cb.prune(new Set())
    expect(cb.hasContent.value).toBe(false)
  })

  it('prune 无变化时不改写存储（避免无谓写入）', () => {
    const cb = useMediaClipboard()
    cb.syncKey('1', 'u1')
    cb.write(payload())
    const before = cb.payload.value

    const pruned = cb.prune(new Set(['a', 'b']))
    expect(pruned).toBe(before)
  })

  it('未 syncKey 时 write 不抛错（只是不落盘）', () => {
    const cb = useMediaClipboard()
    expect(() => cb.write(payload())).not.toThrow()
    expect(cb.hasContent.value).toBe(true)
  })

  it('存储被禁用时退化为内存剪贴板（隐私模式）', () => {
    const original = Object.getOwnPropertyDescriptor(window, 'sessionStorage')
    Object.defineProperty(window, 'sessionStorage', {
      value: {
        getItem() { throw new Error('denied') },
        setItem() { throw new Error('denied') },
        removeItem() { throw new Error('denied') },
        clear() { throw new Error('denied') },
      },
      configurable: true,
    })
    try {
      const cb = useMediaClipboard()
      expect(() => cb.syncKey('1', 'u1')).not.toThrow()
      expect(() => cb.write(payload())).not.toThrow()
      // 内存里那份仍然可用 —— 本次会话的复制粘贴不该因为存储不可用而失效
      expect(cb.hasContent.value).toBe(true)
    } finally {
      if (original) Object.defineProperty(window, 'sessionStorage', original)
    }
  })
})
