/**
 * 全局快捷键目录：**唯一的事实来源**。
 *
 * 为什么单独一个文件：同一份定义有两个消费者 —— `ChatView.vue` 拿它生成真实绑定，
 * 设置页拿它渲染改键面板。若各自抄一份，改键之后两边就会对不上（设置页显示 A、
 * 实际生效 B），而且新增快捷键必然漏改一处。这里只放**静态元数据**，
 * 不含 handler —— handler 属于各自页面。
 */
import type { ShortcutDef } from '../composables/useKeyboardShortcuts'

/** 分组只影响设置页的排版，不参与匹配。 */
export type ShortcutGroup = 'navigation' | 'conversation' | 'panel'

export interface ShortcutSpec extends ShortcutDef {
  /** 文案的 i18n key（settings 域），三语言必须都有 */
  labelKey: string
  group: ShortcutGroup
}

/**
 * 声明顺序 = **Esc 的逐层关闭优先级**（见 useKeyboardShortcuts 文件头约定 1）。
 * 调整顺序即调整优先级，改动前先想清楚"用户此刻最想关掉什么"。
 *
 * 用 `as const satisfies` 而不是普通类型标注：这样 `id` 保留字面量类型，
 * 下面的 :type:`ShortcutId` 才能在**编译期**挡住写错的 id（如 `'close-pannel'`），
 * 而不是等到运行时才发现那条第快捷键静默失效。
 */
const CATALOG = [
  // 速查表自己是最上层浮层：Esc 必须先关它，否则打开速查表后按 Esc 反应的是下层
  {
    id: 'close-shortcuts-help',
    defaultKeys: 'escape',
    labelKey: 'settings.shortcut_close_shortcuts_help',
    group: 'panel',
    customizable: false,
  },
  {
    id: 'open-session-list',
    defaultKeys: 'mod+k',
    labelKey: 'settings.shortcut_open_session_list',
    group: 'navigation',
  },
  {
    id: 'find-in-transcript',
    defaultKeys: 'mod+f',
    labelKey: 'settings.shortcut_find_in_transcript',
    group: 'navigation',
  },
  {
    id: 'show-shortcuts',
    defaultKeys: 'shift+?',
    labelKey: 'settings.shortcut_show_shortcuts',
    group: 'navigation',
  },
  // ── 以下四条同为 Esc，顺序即优先级 ──
  {
    id: 'close-session-map',
    defaultKeys: 'escape',
    labelKey: 'settings.shortcut_close_session_map',
    group: 'conversation',
  },
  {
    id: 'stop-generation',
    defaultKeys: 'escape',
    labelKey: 'settings.shortcut_stop_generation',
    group: 'conversation',
  },
  {
    id: 'close-overlays',
    defaultKeys: 'escape',
    labelKey: 'settings.shortcut_close_overlays',
    group: 'panel',
  },
  {
    id: 'close-panel',
    defaultKeys: 'escape',
    labelKey: 'settings.shortcut_close_panel',
    group: 'panel',
  },
] as const satisfies readonly ShortcutSpec[]

/**
 * 对外暴露的目录。
 *
 * 这里刻意**再导出一次宽类型**（而不是直接用 `CATALOG`）：
 * `as const` 会让每个成员保留精确形状 —— 没写 `customizable` 的成员就不存在这个属性，
 * 于是 `spec.customizable` 在联合类型上会报 TS2339。宽类型 `ShortcutSpec` 声明了
 * `customizable?: boolean`，消费者才能统一读取。
 *
 * 字面量 id 的信息不丢：`ShortcutId` 仍从 `CATALOG` 上取。
 */
export const SHORTCUT_CATALOG: readonly ShortcutSpec[] = CATALOG

/** 目录里所有合法 id；`specOf()` 用它做编译期校验。 */
export type ShortcutId = (typeof CATALOG)[number]['id']

export const SHORTCUT_GROUP_LABEL_KEY: Record<ShortcutGroup, string> = {
  navigation: 'settings.shortcuts_group_navigation',
  conversation: 'settings.shortcuts_group_conversation',
  panel: 'settings.shortcuts_group_panel',
}
