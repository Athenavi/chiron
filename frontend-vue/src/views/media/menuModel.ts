/**
 * 媒体页右键菜单的**求值层**（纯函数，无 Vue / DOM 依赖）。
 *
 * 为什么菜单要单独求值而不是在模板里写 v-if：
 *
 * 1. **禁用必须带原因**。Windows 里灰色的菜单项会告诉你为什么不能点（"需要先选择一项"），
 *    而只写 `:disabled` 的界面是沉默的 —— 用户点不动、不知道为什么、只能来问。
 *    原因以 i18n key 形式给出，由渲染层翻译。
 * 2. **规则要可测**。`open` 只对单项可用、`paste` 依赖剪贴板、`rename` 对多选不可用 ——
 *    这些组合散落在模板里就没法单测，而它们恰恰是最容易写错条件的地方（`>= 1` 写成 `> 1`
 *    这类错误，在界面上的表现只是"偶尔点不动"）。
 * 3. **同一套规则要供三种入口复用**：右键菜单、键盘快捷键（F2/Delete/F5/Alt+Enter）、
 *    工具栏按钮。三处各写一遍条件，迟早互相漂移。
 */

/** 菜单动作。渲染层按它分派，求值层只决定可用性。 */
export type MenuAction =
  | 'open'
  | 'copy'
  | 'cut'
  | 'paste'
  | 'delete'
  | 'rename'
  | 'refresh'
  | 'properties'
  | 'new-folder'
  | 'upload'
  | 'select-all'

/** 求值所需的上下文。全部是显式入参，便于测试构造。 */
export interface MenuContext {
  /** 当前选中项数量 */
  selectedCount: number
  /** 选中项里是否含文件夹 */
  hasFolder: boolean
  /** 选中项里是否含文件（非文件夹） */
  hasFile: boolean
  /** 剪贴板：条目数与模式。空剪贴板表示"无可粘贴"。 */
  clipboard: { count: number; mode: 'copy' | 'cut' | null }
  /**
   * 后端是否已支持**复制**（`POST /v1/media/copy`）。
   *
   * 这是**能力开关而不是功能开关**：复制需要后端新建一份资产，接口尚未实现。
   * 若让菜单项"可点但点了报 404"，用户会认为是 bug；置灰并给出原因是诚实的做法。
   */
  copySupported: boolean
  /**
   * 后端是否支持**移动**（`PUT /v1/media/{id}` 改 `parent_id`）。
   *
   * 与 `copySupported` **必须分开**：剪切/粘贴的落地动作是"移动"，它复用已有的
   * 更新接口，可以立即工作；只有"复制"需要新接口。把三者绑在同一个开关上，
   * 会让本来能用的剪切/粘贴一起变灰 —— 那是把"缺一个功能"放大成"缺三个"。
   */
  moveSupported: boolean
}

/** 求值结果：是否可见、是否可用、以及不可用的原因（i18n key）。 */
export interface MenuEntry {
  action: MenuAction
  /** 不可用原因（i18n key）；为空表示可用 */
  disabledReason: string
  /** 是否在当前上下文里显示（有些项只在选中时才出现） */
  visible: boolean
}

/** 一项菜单的完整求值结果集合（按固定顺序）。 */
export type MenuModel = MenuEntry[]

/** 不可用原因的 i18n key。集中在这里，渲染层不需要再判断任何条件。 */
export const REASON = {
  needOne: 'media.menu_need_one',
  needSelection: 'media.menu_need_selection',
  clipboardEmpty: 'media.menu_clipboard_empty',
  copyUnavailable: 'media.menu_copy_unavailable',
  moveUnavailable: 'media.menu_move_unavailable',
  pasteUnavailable: 'media.menu_paste_unavailable',
  mixedSelection: 'media.menu_mixed_selection',
} as const

/**
 * 粘贴不可用的原因（空串表示可用）。
 *
 * 取决于**剪贴板内容的模式**，而不是一个笼统的开关：
 *  - 复制 → 需要后端有复制接口（尚未实现）；
 *  - 剪切 → 落地是"移动"，只需 `moveSupported`（已有接口，可立即工作）。
 *
 * 把两者合并成一个开关，会让"剪切"这个本来能用的功能跟着"复制"一起失效 ——
 * 那是把一个缺失放大成两个。而**返回原因键**而不是布尔值，是为了让用户看到的
 * 提示指向真正缺失的能力（"暂不支持复制" vs "暂不支持移动"），
 * 而不是一句含糊的"暂不支持此粘贴操作"。
 */
function pasteUnavailableReason(ctx: MenuContext): string {
  // 剪贴板为空的情形由调用方先判（那是另一种原因）
  if (ctx.clipboard.mode === 'cut') {
    return ctx.moveSupported ? '' : REASON.moveUnavailable
  }
  return ctx.copySupported ? '' : REASON.copyUnavailable
}

/**
 * 求值整张菜单。
 *
 * 顺序刻意按 Windows 资源管理器的分组习惯：打开 / 剪贴板 / 重命名删除 / 刷新属性 / 新建。
 * 顺序固定而不是动态排序 —— 菜单项位置会随选择变化而跳动时，肌肉记忆完全失效。
 */
export function buildMenuModel(ctx: MenuContext): MenuModel {
  const one = ctx.selectedCount === 1
  const any = ctx.selectedCount > 0

  // 复制/剪切对混合选择是合法的（Windows 支持同时复制文件和文件夹），
  // 因此这里**不**因 hasFolder && hasFile 而禁用；只有"重命名/打开/属性"这类
  // 依赖单个身份的操作才不允许混合。
  const clipboardReady = ctx.clipboard.count > 0

  return [
    {
      action: 'open',
      visible: any,
      disabledReason: !one
        ? (ctx.selectedCount === 0 ? REASON.needSelection : REASON.needOne)
        : '',
    },
    {
      action: 'copy',
      visible: any,
      // 三项独立原因：没选东西 / 后端不支持复制。混成一句会让"为什么灰色"无法区分。
      disabledReason: !any
        ? REASON.needSelection
        : (!ctx.copySupported ? REASON.copyUnavailable : ''),
    },
    {
      action: 'cut',
      visible: any,
      // 剪切落地是"移动"，走已有的 PUT /v1/media/{id}，因此不依赖 copySupported
      disabledReason: !any
        ? REASON.needSelection
        : (!ctx.moveSupported ? REASON.moveUnavailable : ''),
    },
    {
      action: 'paste',
      // 粘贴与选择无关：空白处右键也要能粘贴，否则用户必须"先随便选一个"才能贴。
      visible: true,
      // 不可用原因**按剪贴板模式**给出：复制态缺的是"复制接口"，剪切态缺的是"移动接口"。
      // 统一报一个"暂不支持粘贴"会让用户不知道究竟是哪个能力缺失 —— 而这两件事的
      // 后续动作完全不同（等复制接口 / 换个目录再试）。
      disabledReason: !clipboardReady
        ? REASON.clipboardEmpty
        : (pasteUnavailableReason(ctx)),
    },
    {
      action: 'rename',
      visible: any,
      disabledReason: !one
        ? (ctx.selectedCount === 0 ? REASON.needSelection : REASON.needOne)
        : '',
    },
    {
      action: 'delete',
      visible: any,
      disabledReason: !any ? REASON.needSelection : '',
    },
    {
      action: 'refresh',
      visible: true,
      disabledReason: '',
    },
    {
      action: 'properties',
      visible: any,
      // 属性对多选没有意义（显示谁的大小？）。真要统计多选总量是另一个功能。
      disabledReason: !one
        ? (ctx.selectedCount === 0 ? REASON.needSelection : REASON.needOne)
        : '',
    },
    {
      action: 'new-folder',
      visible: true,
      disabledReason: '',
    },
    {
      action: 'upload',
      visible: true,
      disabledReason: '',
    },
    {
      action: 'select-all',
      visible: true,
      disabledReason: '',
    },
  ]
}

/** 取某个动作的求值结果（键盘快捷键用；不存在时返回 undefined）。 */
export function entryOf(model: MenuModel, action: MenuAction): MenuEntry | undefined {
  return model.find((entry) => entry.action === action)
}

/** 该动作在当前上下文里能否执行（不可见或带原因都算不能）。 */
export function canRun(model: MenuModel, action: MenuAction): boolean {
  const entry = entryOf(model, action)
  return !!entry && entry.visible && entry.disabledReason === ''
}

/**
 * 删除操作的**危险等级**。用于决定确认对话框的形态。
 *
 * 分级依据是"可恢复成本"而不是项数本身：删一个空文件夹与删一棵 47 项的树
 * 在用户心理上完全不同，而对话框如果长得一样，用户会条件反射点"确定"。
 */
export type DangerLevel = 'plain' | 'strong'

export interface DeleteScope {
  selectedCount: number
  hasFolder: boolean
  /** 待删除的递归总项数（含子孙）。未知时传 null。 */
  descendantCount: number | null
}

export function deleteDanger(scope: DeleteScope): DangerLevel {
  if (scope.hasFolder) return 'strong'
  if (scope.selectedCount > 5) return 'strong'
  return 'plain'
}

// ─────────────────────────────────────────────────────────────
// 菜单的**项列表**（分组、文案键、快捷键）
// ─────────────────────────────────────────────────────────────
//
// 单独放在这里而不是组件内部，是因为组件的浮层是**懒渲染**的：antd 的 Dropdown
// 打开之前不会挂载 overlay，因此"渲染出来的菜单项对不对"没法在组件测试里直接断言
// （`findComponent(Menu)` 拿到的是空 wrapper）。把"有哪些项、什么顺序、分几组"
// 抽成纯函数后，这些都能直接测；组件只剩下"映射成 antd 结构 + 派发点击"。

/** 动作 → 文案 i18n key。 */
export const LABEL_KEYS: Record<MenuAction, string> = {
  open: 'media.menu_open',
  copy: 'common.copy',
  cut: 'media.menu_cut',
  paste: 'media.menu_paste',
  delete: 'common.delete',
  rename: 'common.rename',
  refresh: 'common.refresh',
  properties: 'media.menu_properties',
  'new-folder': 'media.menu_new_folder',
  upload: 'media.menu_upload',
  'select-all': 'common.select_all',
}

/**
 * 动作 → 快捷键提示。
 *
 * 显示它们不是为了好看：右键菜单是用户**发现**快捷键的主要途径 ——
 * 不显示的话，F2 / Delete / Alt+Enter 这些更快的路径永远只有老用户知道。
 */
export const SHORTCUTS: Partial<Record<MenuAction, string>> = {
  copy: 'Ctrl+C',
  cut: 'Ctrl+X',
  paste: 'Ctrl+V',
  delete: 'Delete',
  rename: 'F2',
  refresh: 'F5',
  properties: 'Alt+Enter',
  'select-all': 'Ctrl+A',
}

/**
 * 分组：组间插分割线。
 *
 * 顺序固定而不是按可用性重排 —— 菜单项位置随选择跳动时，肌肉记忆完全失效。
 * 分组方式对齐 Windows 资源管理器：打开 / 剪贴板 / 改名删除 / 查看 / 新建。
 */
export const MENU_GROUPS: readonly (readonly MenuAction[])[] = [
  ['open'],
  ['copy', 'cut', 'paste'],
  ['rename', 'delete'],
  ['refresh', 'properties'],
  ['new-folder', 'upload', 'select-all'],
]

/** 渲染所需的扁平描述（已剔除不可见项、标记分组边界）。 */
export interface MenuDescriptor {
  action: MenuAction
  labelKey: string
  /** 空串表示该动作没有登记的快捷键 */
  shortcut: string
  disabled: boolean
  /** 不可用原因的 i18n key；空串表示可用 */
  reasonKey: string
  dividerBefore: boolean
}

/**
 * 把求值结果展平成渲染描述。
 *
 * 规则：
 *  - 不可见项**整条剔除**（而不是隐藏后留白）；
 *  - 分组内全不可见时不产生空的分割线（否则菜单里会出现连续两条横线）；
 *  - 第一项之前不插分割线。
 */
export function buildMenuDescriptors(model: MenuModel): MenuDescriptor[] {
  const out: MenuDescriptor[] = []
  let first = true
  for (const group of MENU_GROUPS) {
    const rendered: MenuDescriptor[] = []
    for (const action of group) {
      const entry = entryOf(model, action)
      if (!entry || !entry.visible) continue
      rendered.push({
        action,
        labelKey: LABEL_KEYS[action],
        shortcut: SHORTCUTS[action] ?? '',
        disabled: entry.disabledReason !== '',
        reasonKey: entry.disabledReason,
        dividerBefore: false,
      })
    }
    if (rendered.length === 0) continue
    if (!first) rendered[0]!.dividerBefore = true
    first = false
    out.push(...rendered)
  }
  return out
}
