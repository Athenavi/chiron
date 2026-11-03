/**
 * 属性面板的**字段构造**（纯函数，无 Vue / DOM 依赖）。
 *
 * 现有详情抽屉直接渲染了 `detailItem.created_at` 的原始值（`2026-11-03T11:32:26.675Z`
 * 这种形态），而项目其他页面早已统一走 `utils/datetime` 的 `formatDateTime`。
 * 同一份时间在两处长得不一样，用户会以为是两个不同的时间。
 *
 * 三个刻意的取舍：
 *
 * 1. **文件夹不显示大小**。文件夹的 `size` 是 0，显示成 "0 B" 会被读成"空文件夹"——
 *    而那通常是"没统计过"，不是"里面没东西"。显示 "—" 才不误导。
 * 2. **路径从面包屑拼**。用户最常问的是"这个文件在哪"，而列表只显示当前层。
 * 3. **每行都带可复制性**：属性的实际用途是"拿去别处用"（路径、URL、id）。
 */

import type { MenuAction } from './menuModel'

/** 构造属性行所需的源数据（与 `MediaItem` 的字段对齐，但不依赖它的类型）。 */
export interface PropertySource {
  id: string
  name: string
  type: string
  size?: number
  mime_type?: string
  category?: string
  file_url?: string
  created_at?: string
  updated_at?: string
  tags?: string[]
  /** 所属文件夹的路径（不含自己），如 `['reports', '2024']` */
  parentPath?: string[]
}

/** 一行属性。 */
export interface PropertyRow {
  /** 稳定 key（渲染列表用） */
  key: string
  /** 标签的 i18n key */
  labelKey: string
  /** 值的展示文本（已格式化） */
  value: string
  /** 可复制：渲染层据此给一个复制按钮 */
  copyable: boolean
  /** 单行省略（路径/URL 这类长值） */
  truncate: boolean
}

/** 判断是否文件夹。 */
export function isFolder(source: Pick<PropertySource, 'type'>): boolean {
  return source.type === 'folder'
}

/**
 * 人类可读的大小。
 *
 * 与 `MediaView` 原有的 `formatSize` 保持同一口径（1024 进制、一位小数），
 * 但这里多处理两件它没做的事：
 *  - 0 与「未知」分开：文件夹/未统计传 `undefined`，显示 `—`；
 *  - `bytes = 0` 的文件显示 `0 B`（一个真的空文件），不是 `—`。
 */
export function formatSize(bytes: number | undefined, folder: boolean): string {
  if (folder) return '—'
  if (bytes === undefined) return '—'
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  // 字节不显示小数（`1024.0 B` 这种没有意义）
  return unit === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${units[unit]}`
}

/**
 * 构造完整路径：`reports/2024/q3.md`。
 *
 * 没有 parentPath 时返回名字本身（根目录下的文件），而不是 `undefined` ——
 * 属性面板上留空会让用户以为"没有路径"。
 */
export function fullPath(source: Pick<PropertySource, 'name' | 'parentPath'>): string {
  const parts = [...(source.parentPath ?? []), source.name].filter(Boolean)
  return parts.join('/')
}

/**
 * 构造属性行列表。
 *
 * `formatDateTime` 由调用方注入而不是在这里 import：这个模块要保持零依赖，
 * 才能在测试里不触碰 i18n / dayjs 的 locale 初始化。
 */
export function buildPropertyRows(
  source: PropertySource,
  formatDateTime: (value: string | undefined) => string,
): PropertyRow[] {
  const folder = isFolder(source)
  const rows: PropertyRow[] = [
    {
      key: 'name',
      labelKey: 'common.name',
      value: source.name,
      copyable: false,
      truncate: false,
    },
    {
      key: 'path',
      labelKey: 'media.property_path',
      value: fullPath(source),
      copyable: true,
      truncate: true,
    },
    {
      key: 'type',
      labelKey: 'common.type',
      value: folder ? '—' : (source.type || '—'),
      copyable: false,
      truncate: false,
    },
    {
      key: 'size',
      labelKey: 'common.size',
      // 文件夹显示 —（0 B 会被读成"空文件夹"）
      value: formatSize(source.size, folder),
      copyable: false,
      truncate: false,
    },
  ]

  // MIME 只对文件有意义
  if (!folder) {
    rows.push({
      key: 'mime',
      labelKey: 'media.property_mime',
      value: source.mime_type || '—',
      copyable: false,
      truncate: false,
    })
  }

  if (source.category) {
    rows.push({
      key: 'category',
      labelKey: 'common.category',
      value: source.category,
      copyable: false,
      truncate: false,
    })
  }

  rows.push(
    {
      key: 'created',
      labelKey: 'common.created_at',
      value: formatDateTime(source.created_at),
      copyable: false,
      truncate: false,
    },
    {
      key: 'updated',
      labelKey: 'common.updated_at',
      // 从未更新过时回落创建时间：显示 "—" 会让人以为数据缺失
      value: formatDateTime(source.updated_at || source.created_at),
      copyable: false,
      truncate: false,
    },
  )

  if (source.tags && source.tags.length > 0) {
    rows.push({
      key: 'tags',
      labelKey: 'common.tag',
      value: source.tags.join(', '),
      copyable: true,
      truncate: true,
    })
  }

  rows.push({
    key: 'id',
    labelKey: 'media.property_id',
    value: source.id,
    copyable: true,
    truncate: false,
  })

  // URL 只对文件有意义（文件夹没有可下载地址）
  if (!folder && source.file_url) {
    rows.push({
      key: 'url',
      labelKey: 'media.property_url',
      value: source.file_url,
      copyable: true,
      truncate: true,
    })
  }

  return rows
}

/**
 * 属性面板底部可执行的动作。
 *
 * 与右键菜单**共用同一套动作名**：这样"菜单里能做什么"与"属性面板里能做什么"
 * 不会各写一份而后漂移。求值仍交给 `menuModel`，这里只决定"属性面板放哪些"。
 */
export const PROPERTY_ACTIONS: readonly MenuAction[] = [
  'open',
  'rename',
  'copy',
  'delete',
]
