import { describe, expect, it } from 'vitest'

import {
  buildPropertyRows,
  formatSize,
  fullPath,
  isFolder,
  PROPERTY_ACTIONS,
  type PropertySource,
} from '../properties'

/**
 * 属性字段构造的用例。
 *
 * 重点在两个"看起来对、实际误导"的地方：
 *  - 文件夹显示 `0 B` 会被读成"空文件夹"，而真相是"没统计"；
 *  - 日期直接渲染原始 ISO 串，与项目其他页面的格式不一致，用户以为是两个时间。
 */

/** 注入固定的格式化器，避免为了测属性去初始化 dayjs 的 locale。 */
const fixedTime = (v: string | undefined) => (v ? `T:${v}` : '—')

const file = (override: Partial<PropertySource> = {}): PropertySource => ({
  id: 'id-1',
  name: 'q3.md',
  type: 'text',
  size: 2048,
  mime_type: 'text/markdown',
  created_at: '2026-11-03T11:32:26Z',
  updated_at: '2026-11-04T09:00:00Z',
  ...override,
})

const folder = (override: Partial<PropertySource> = {}): PropertySource => ({
  id: 'f-1',
  name: 'reports',
  type: 'folder',
  size: 0,
  ...override,
})

const rowOf = (rows: ReturnType<typeof buildPropertyRows>, key: string) =>
  rows.find((r) => r.key === key)

describe('isFolder', () => {
  it('type=folder 才算文件夹', () => {
    expect(isFolder({ type: 'folder' })).toBe(true)
    expect(isFolder({ type: 'text' })).toBe(false)
    expect(isFolder({ type: '' })).toBe(false)
  })
})

describe('formatSize', () => {
  it('文件夹显示 —，而不是 0 B', () => {
    // `0 B` 会被读成"空文件夹"，真相是"没统计过"
    expect(formatSize(0, true)).toBe('—')
    expect(formatSize(1024, true)).toBe('—')
  })

  it('未统计（undefined）显示 —', () => {
    expect(formatSize(undefined, false)).toBe('—')
  })

  it('0 字节的**文件**显示 0 B（它是真的空文件）', () => {
    expect(formatSize(0, false)).toBe('0 B')
  })

  it('按 1024 进制进位', () => {
    expect(formatSize(512, false)).toBe('512 B')
    expect(formatSize(1024, false)).toBe('1.0 KB')
    expect(formatSize(1536, false)).toBe('1.5 KB')
    expect(formatSize(1048576, false)).toBe('1.0 MB')
  })

  it('字节级别不显示小数', () => {
    // `1024.0 B` 这种没有意义
    expect(formatSize(999, false)).toBe('999 B')
  })
})

describe('fullPath', () => {
  it('拼出完整路径', () => {
    expect(fullPath({ name: 'q3.md', parentPath: ['reports', '2024'] })).toBe('reports/2024/q3.md')
  })

  it('根目录下的文件返回名字本身（不留空）', () => {
    // 留空会让用户以为"没有路径"
    expect(fullPath({ name: 'README.md' })).toBe('README.md')
    expect(fullPath({ name: 'README.md', parentPath: [] })).toBe('README.md')
  })

  it('过滤空段（避免出现双斜杠）', () => {
    expect(fullPath({ name: 'a.md', parentPath: ['', 'x'] })).toBe('x/a.md')
  })
})

describe('buildPropertyRows（文件）', () => {
  const rows = buildPropertyRows(file(), fixedTime)

  it('包含基础字段', () => {
    for (const key of ['name', 'path', 'type', 'size', 'mime', 'created', 'updated', 'id']) {
      expect(rowOf(rows, key)).toBeDefined()
    }
  })

  it('大小按人类可读格式', () => {
    expect(rowOf(rows, 'size')?.value).toBe('2.0 KB')
  })

  it('MIME 原样展示', () => {
    expect(rowOf(rows, 'mime')?.value).toBe('text/markdown')
  })

  it('时间走注入的格式化器（不渲染原始 ISO 串）', () => {
    expect(rowOf(rows, 'created')?.value).toBe('T:2026-11-03T11:32:26Z')
    expect(rowOf(rows, 'updated')?.value).toBe('T:2026-11-04T09:00:00Z')
  })

  it('没有 updated_at 时回落创建时间（不显示 — 让人以为数据缺失）', () => {
    const r = buildPropertyRows(file({ updated_at: undefined }), fixedTime)
    expect(rowOf(r, 'updated')?.value).toBe('T:2026-11-03T11:32:26Z')
  })

  it('路径、id 可复制；名称不可复制（复制名字意义不大）', () => {
    expect(rowOf(rows, 'path')?.copyable).toBe(true)
    expect(rowOf(rows, 'id')?.copyable).toBe(true)
    expect(rowOf(rows, 'name')?.copyable).toBe(false)
  })

  it('长值标记为需要省略（路径/URL）', () => {
    expect(rowOf(rows, 'path')?.truncate).toBe(true)
  })

  it('有 file_url 时出现下载地址行', () => {
    const r = buildPropertyRows(file({ file_url: '/media/1/x/q3.md' }), fixedTime)
    expect(rowOf(r, 'url')?.value).toBe('/media/1/x/q3.md')
  })

  it('有标签时出现标签行', () => {
    const r = buildPropertyRows(file({ tags: ['a', 'b'] }), fixedTime)
    expect(rowOf(r, 'tags')?.value).toBe('a, b')
  })

  it('无标签时不出现空标签行', () => {
    expect(rowOf(rows, 'tags')).toBeUndefined()
  })

  it('有 category 时才出现分类行', () => {
    expect(rowOf(rows, 'category')).toBeUndefined()
    const r = buildPropertyRows(file({ category: 'code' }), fixedTime)
    expect(rowOf(r, 'category')?.value).toBe('code')
  })
})

describe('buildPropertyRows（文件夹）', () => {
  const rows = buildPropertyRows(folder(), fixedTime)

  it('不出现 MIME 行（文件夹没有 MIME）', () => {
    expect(rowOf(rows, 'mime')).toBeUndefined()
  })

  it('不出现下载地址行', () => {
    expect(rowOf(rows, 'url')).toBeUndefined()
  })

  it('大小显示 —', () => {
    expect(rowOf(rows, 'size')?.value).toBe('—')
  })

  it('类型显示 —（folder 这个内部值对用户没意义）', () => {
    expect(rowOf(rows, 'type')?.value).toBe('—')
  })

  it('仍包含路径与创建时间', () => {
    expect(rowOf(rows, 'path')).toBeDefined()
    expect(rowOf(rows, 'created')).toBeDefined()
  })
})

describe('buildPropertyRows（健壮性）', () => {
  it('字段全缺时不抛错，缺失值以 — 呈现', () => {
    const rows = buildPropertyRows({ id: 'x', name: 'n', type: 'file' }, fixedTime)
    expect(rowOf(rows, 'size')?.value).toBe('—')
    expect(rowOf(rows, 'mime')?.value).toBe('—')
    expect(rowOf(rows, 'created')?.value).toBe('—')
  })

  it('行 key 唯一（渲染列表不会 key 冲突）', () => {
    const rows = buildPropertyRows(
      file({ tags: ['t'], category: 'c', file_url: '/m/x' }),
      fixedTime,
    )
    const keys = rows.map((r) => r.key)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('每行的 labelKey 指向真实命名空间', () => {
    const rows = buildPropertyRows(file({ tags: ['t'], file_url: '/m/x' }), fixedTime)
    for (const row of rows) {
      expect(row.labelKey).toMatch(/^(common|media)\./)
    }
  })
})

describe('PROPERTY_ACTIONS', () => {
  it('都是合法的菜单动作（与右键菜单共用同一套求值）', () => {
    // 共用动作名是为了让"菜单能做什么"与"属性面板能做什么"不会各写一份而后漂移
    const allowed = new Set(['open', 'rename', 'copy', 'delete'])
    for (const action of PROPERTY_ACTIONS) {
      expect(allowed.has(action)).toBe(true)
    }
  })
})
