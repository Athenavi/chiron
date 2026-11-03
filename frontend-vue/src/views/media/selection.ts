/**
 * 媒体页的**选择状态机**（纯函数，无 Vue / DOM 依赖）。
 *
 * 为什么单独抽出来：框选、Shift 连选、Ctrl 切换这三件事的正确性全都是**边界**问题 ——
 * 「拖到一半反向拉」「Shift 跨行跨列」「先 Ctrl 取消再 Shift 选」这些组合靠手点几乎
 * 测不出来，而一旦写进组件里就再也无法单测。抽成纯函数后，每条边界都能用三行断言钉住。
 *
 * 三条与 Windows 资源管理器对齐的语义（不是自创）：
 *
 * 1. **anchor（锚点）是独立状态**，不等于"最后选中的那一个"。Shift 连选永远相对于
 *    anchor 计算；Plain 单击与 Ctrl 单击都会把 anchor 移到被点项上 —— 这是 Windows 的
 *    实际行为，也是"连点两次后再 Shift"结果可预测的前提。
 * 2. **Shift 连选按 items 的当前顺序**，不是 DOM 顺序。列表视图与网格视图排序可能不同，
 *    拿 DOM 位置算区间会在排序切换后选错一片。
 * 3. **框选只命中"完全落入矩形"的项**。与项相交但不被包含的不选 —— 这是资源管理器
 *    的行为，也避免了"碰到边就选中"的误选。
 */

/** 一份可被选择的最小形状（只依赖 id，便于在网格/列表/测试里复用）。 */
export interface Selectable {
  id: string
}

/** 选择的完整状态：`selected` 是集合，`anchorId` 是 Shift 连选的基准。 */
export interface SelectionState {
  readonly selected: ReadonlySet<string>
  readonly anchorId: string | null
}

/** 点击时按下的修饰键。`shift-ctrl` 是"把区间**追加**进现有选择"。 */
export type ClickModifier = 'none' | 'ctrl' | 'shift' | 'shift-ctrl'

/** 空选择。 */
export const EMPTY_SELECTION: SelectionState = {
  selected: new Set<string>(),
  anchorId: null,
}

/**
 * 归一化修饰键。调用方只需把 event 的三个布尔值传进来，不必自己判断组合。
 */
export function modifierOf(event: {
  ctrlKey?: boolean
  shiftKey?: boolean
  metaKey?: boolean
}): ClickModifier {
  // mac 的 Command 等价于 Windows 的 Ctrl（都表示"追加/切换"）——
  // 只认 ctrlKey 会让 mac 上的多选完全失效，而这类差异用户不会来报 bug，只会觉得"不好用"。
  const additive = !!(event.ctrlKey || event.metaKey)
  if (event.shiftKey) return additive ? 'shift-ctrl' : 'shift'
  return additive ? 'ctrl' : 'none'
}

/** 用新集合替换 `selected`，同时改锚点。所有分支都走它，保证返回对象永远是新建的。 */
function next(ids: Iterable<string>, anchorId: string | null): SelectionState {
  // 必须换一个新 Set：Vue 对同一个 Set 实例的原地修改不会触发依赖更新
  // （`ref(new Set())` 的深层响应式对被方法修改的集合并不可靠），
  // 而 `selected.size` 之类的读取就再也不更新，表现为"选中了但界面没变"。
  return { selected: new Set(ids), anchorId }
}

/**
 * 处理一次点击。
 *
 * - `none`：只选这一项（清空其它），锚点移到它
 * - `ctrl`：切换这一项；锚点移到它（Windows 行为）
 * - `shift`：选中 anchor→id 的闭区间（替换现有选择）
 * - `shift-ctrl`：把该区间**追加**进现有选择
 *
 * 锚点缺失（首次点击、或刷新后锚点项已被删除）时，Shift 退化为普通单击 ——
 * 这是唯一不会让人意外的处理：没有基准时"连选"没有定义。
 */
export function clickSelect(
  items: readonly Selectable[],
  state: SelectionState,
  id: string,
  modifier: ClickModifier = 'none',
): SelectionState {
  if (modifier === 'none') {
    return next([id], id)
  }

  if (modifier === 'ctrl') {
    const selected = new Set(state.selected)
    if (selected.has(id)) selected.delete(id)
    else selected.add(id)
    return next(selected, id)
  }

  // shift / shift-ctrl：需要锚点与目标都在当前列表里才能算区间
  const anchorIndex = state.anchorId ? indexOf(items, state.anchorId) : -1
  const targetIndex = indexOf(items, id)
  if (anchorIndex < 0 || targetIndex < 0) {
    // 没有可用锚点：按普通单击处理（保留原有选择与否以 shift-ctrl 决定）
    return modifier === 'shift-ctrl'
      ? next([...state.selected, id], id)
      : next([id], id)
  }

  const [from, to] = anchorIndex <= targetIndex
    ? [anchorIndex, targetIndex]
    : [targetIndex, anchorIndex]
  const range: string[] = []
  for (let i = from; i <= to; i += 1) {
    const item = items[i]
    if (item) range.push(item.id)
  }

  // Shift 连选**不改锚点**：它是"从基准拉到这里"，基准本身不该跳走，
  // 否则连按两次 Shift 会像滚雪球一样越选越偏。
  if (modifier === 'shift-ctrl') {
    return next([...state.selected, ...range], state.anchorId)
  }
  return next(range, state.anchorId)
}

function indexOf(items: readonly Selectable[], id: string): number {
  for (let i = 0; i < items.length; i += 1) {
    if (items[i]?.id === id) return i
  }
  return -1
}

/** 轴对齐矩形（视口坐标）。 */
export interface Rect {
  left: number
  top: number
  right: number
  bottom: number
}

/** 每个候选项在视口里的包围盒。 */
export interface ItemRect extends Rect {
  id: string
}

/**
 * 把拖拽的起点与当前点归一化成矩形。
 *
 * 必须归一化：用户会从右下往左上拖，此时 `endX - startX` 是负数，
 * 直接拿去比较会让"完全落入"判断永远为假 —— 表现为"往一个方向拖能选、反方向拖选不中"。
 */
export function normalizeRect(
  start: { x: number; y: number },
  end: { x: number; y: number },
): Rect {
  return {
    left: Math.min(start.x, end.x),
    top: Math.min(start.y, end.y),
    right: Math.max(start.x, end.x),
    bottom: Math.max(start.y, end.y),
  }
}

/** `inner` 是否完全落在 `outer` 内（含边界，触碰即算）。 */
export function containsRect(outer: Rect, inner: Rect): boolean {
  return (
    inner.left >= outer.left &&
    inner.right <= outer.right &&
    inner.top >= outer.top &&
    inner.bottom <= outer.bottom
  )
}

/**
 * 框选：返回完全落入矩形的项 id。
 *
 * `additive`（Ctrl 按住）为真时**并入**现有选择，否则替换。
 * 注意"完全落入"而非"相交"：与 Windows 一致，也避免拖拽起点擦过几个卡片就误选它们。
 */
export function marqueeSelect(
  itemRects: readonly ItemRect[],
  rect: Rect,
  state: SelectionState,
  additive = false,
): SelectionState {
  const hits: string[] = []
  for (const item of itemRects) {
    if (containsRect(rect, item)) hits.push(item.id)
  }
  if (additive) {
    return next([...state.selected, ...hits], state.anchorId)
  }
  // 非追加时锚点跟着框选结果走：之后按 Shift 应该从这个新基准开始，
  // 否则会从"上一次单击的项"开始拉区间，选出一片用户没打算要的东西。
  return next(hits, hits.length > 0 ? (hits[0] ?? null) : null)
}

/** 全选。 */
export function selectAll(items: readonly Selectable[], state: SelectionState): SelectionState {
  const ids = items.map((i) => i.id)
  // 锚点缺失时补一个，让全选之后立刻 Shift 也能正常工作
  return next(ids, state.anchorId ?? (ids[0] ?? null))
}

/** 清空。 */
export function clearSelection(): SelectionState {
  return next([], null)
}

/** 反选（保留锚点：它描述的是"基准"，与结果无关）。 */
export function invertSelection(
  items: readonly Selectable[],
  state: SelectionState,
): SelectionState {
  const nextIds: string[] = []
  for (const item of items) {
    if (!state.selected.has(item.id)) nextIds.push(item.id)
  }
  return next(nextIds, state.anchorId)
}

/**
 * 剔除已不存在的项。
 *
 * 为什么必须有：刷新、翻页、删除之后，`selected` 里会残留已经不存在的 id。
 * 后果不是"显示多了个高亮"这么轻 —— 批量删除会把它们一起发出去，而后端对不存在
 * 的 id 静默忽略，用户看到"删了 5 个但提示删了 5 个、实际 3 个"无从判断；
 * 更糟的是粘贴/移动会用这些幽灵 id 去操作，报出一个与实际无关的错误。
 *
 * 锚点一并校验：锚点项被删后仍留着它，Shift 连选会算出一个空区间（看起来"点不动"）。
 */
export function pruneSelection(
  items: readonly Selectable[],
  state: SelectionState,
): SelectionState {
  const alive = new Set(items.map((i) => i.id))
  const kept: string[] = []
  for (const id of state.selected) {
    if (alive.has(id)) kept.push(id)
  }
  const anchorAlive = state.anchorId !== null && alive.has(state.anchorId)

  // 没有变化时返回**原对象**：每次刷新都造新对象会让下游的 watch 空转，
  // 在流式刷新/轮询场景里就是无谓的重渲染。
  if (kept.length === state.selected.size && (anchorAlive || state.anchorId === null)) {
    return state
  }
  return next(kept, anchorAlive ? state.anchorId : null)
}

/** 选中的项（按列表顺序，便于批量操作给出稳定的执行次序）。 */
export function selectedItems<T extends Selectable>(
  items: readonly T[],
  state: SelectionState,
): T[] {
  return items.filter((i) => state.selected.has(i.id))
}

/** 是否只包含文件夹（用于决定菜单项可用性）。 */
export function onlyFolders<T extends Selectable & { type?: string }>(
  items: readonly T[],
  state: SelectionState,
): boolean {
  const picked = selectedItems(items, state)
  return picked.length > 0 && picked.every((i) => i.type === 'folder')
}
