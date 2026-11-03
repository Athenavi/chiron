/**
 * 媒体页的**剪贴板与粘贴冲突**（纯函数，无 Vue / DOM 依赖）。
 *
 * 与 Windows 资源管理器对齐的三条硬约束：
 *
 * 1. **剪贴板活在进程内**。浏览器不允许脚本把文件放进系统剪贴板，所以"复制/粘贴"
 *    只能是应用内语义。用 `sessionStorage` 而不是内存变量：刷新页面后剪贴板仍在，
 *    这与资源管理器的行为一致（用户复制后就切了个标签页回来，不该发现剪贴板空了）。
 * 2. **粘贴前必须重新校验 id**。存下来的是 id，而源项可能在复制之后被删掉。
 *    不校验就会拿幽灵 id 去请求后端，报出一个与当前操作毫无关系的错误。
 * 3. **同名冲突必须让用户选**（替换/跳过/保留两者），不能默默覆盖 ——
 *    静默覆盖是数据丢失，而"保留两者"要给 Windows 式的 `name (2).ext`。
 */

/** 剪贴板里的一份内容。`mode` 决定粘贴是复制还是移动。 */
export interface ClipboardPayload {
  mode: 'copy' | 'cut'
  /** 源项 id（有序：粘贴后仍按这个顺序执行，便于复现问题） */
  ids: string[]
  /** 源所在目录，用于"粘贴到自己所在的目录"这种无效操作的前置判断 */
  fromParentId: string
}

/** 空剪贴板。 */
export const EMPTY_CLIPBOARD: ClipboardPayload = { mode: 'copy', ids: [], fromParentId: '' }

/**
 * sessionStorage 的键。
 *
 * 带 tenant 与 user 前缀：同一浏览器里切换账号时，前一个用户的剪贴板不该被
 * 后一个用户看到 —— 那些 id 属于别人的资产，即便粘贴会失败，泄露"存在哪些 id"
 * 本身也不合适。
 */
export function clipboardStorageKey(tenantId: string, userId: string): string {
  return `chiron.media.clipboard:${tenantId || '-'}:${userId || '-'}`
}

/**
 * 序列化 / 反序列化。
 *
 * 反序列化走**完全防御式**：sessionStorage 里的内容可能被旧版本写过、被手工改过、
 * 或中途结构升级过。任何一项不合法就整体退回空剪贴板 —— 半份剪贴板比没有更危险
 * （粘贴会执行一部分、再报错，用户不知道到底做成了什么）。
 */
export function serializeClipboard(payload: ClipboardPayload): string {
  return JSON.stringify({ mode: payload.mode, ids: payload.ids, fromParentId: payload.fromParentId })
}

export function parseClipboard(raw: string | null | undefined): ClipboardPayload {
  if (!raw) return EMPTY_CLIPBOARD
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return EMPTY_CLIPBOARD
  }
  if (!parsed || typeof parsed !== 'object') return EMPTY_CLIPBOARD
  const obj = parsed as Record<string, unknown>
  const mode = obj.mode === 'cut' ? 'cut' : obj.mode === 'copy' ? 'copy' : null
  const fromParentId = typeof obj.fromParentId === 'string' ? obj.fromParentId : null
  const ids = Array.isArray(obj.ids)
    ? obj.ids.filter((v): v is string => typeof v === 'string' && v !== '')
    : null
  if (!mode || fromParentId === null || ids === null || ids.length === 0) {
    return EMPTY_CLIPBOARD
  }
  return { mode, ids, fromParentId }
}

/**
 * 剔除已不存在的源项（粘贴前调用）。
 *
 * 返回新的载荷；`ids` 全都没了就是空剪贴板 —— 此时 UI 应把"粘贴"置灰，
 * 而不是让用户点了再收到一个"源项不存在"的报错。
 */
export function pruneClipboard(
  payload: ClipboardPayload,
  aliveIds: ReadonlySet<string>,
): ClipboardPayload {
  const alive = payload.ids.filter((id) => aliveIds.has(id))
  if (alive.length === payload.ids.length) return payload
  if (alive.length === 0) return EMPTY_CLIPBOARD
  return { ...payload, ids: alive }
}

/**
 * 生成 Windows 式的"保留两者"名字。
 *
 * 规则（与资源管理器一致）：
 *  - 有扩展名：`report.txt` → `report (2).txt` → `report (3).txt`
 *  - 无扩展名：`notes` → `notes (2)`
 *  - 以点开头的名字（`.gitignore`）**不当作扩展名** —— 它是"隐藏文件名"，
 *    `(.gitignore)` 那种结果在资源管理器里也不会出现。
 *
 * 起点是 2 而不是 1：`report.txt` 已存在时，第一个新名字是 `report (2).txt`。
 */
export function uniqueName(name: string, taken: ReadonlySet<string>): string {
  if (!taken.has(name)) return name

  const { stem, ext } = splitExt(name)
  // 上限防死循环：极端情况下（上千个同名）也不该卡住主线程。
  for (let n = 2; n < 10_000; n += 1) {
    const candidate = `${stem} (${n})${ext}`
    if (!taken.has(candidate)) return candidate
  }
  // 兜底：加一个不冲突的时间戳后缀，保证函数总能返回一个可用名字
  return `${stem} (${Date.now()})${ext}`
}

/** 拆分 `stem` 与 `ext`（含点）。点开头或无点视为无扩展名。 */
export function splitExt(name: string): { stem: string; ext: string } {
  const dot = name.lastIndexOf('.')
  if (dot <= 0 || dot === name.length - 1) return { stem: name, ext: '' }
  return { stem: name.slice(0, dot), ext: name.slice(dot) }
}

/** 粘贴时对单个冲突项的处置。 */
export type ConflictChoice = 'replace' | 'skip' | 'keep-both'

/** 一个源项在目标目录里遇到了同名。 */
export interface Conflict {
  /** 源项 id */
  id: string
  /** 源项名字 */
  name: string
}

/**
 * 检测冲突：返回与目标目录现有名字相撞的源项。
 *
 * 用名字而不是 id 判断：粘贴到**另一个目录**时，id 必然不同（复制会新建 asset），
 * 真正撞的是名字。
 */
export function detectConflicts(
  sources: readonly { id: string; name: string }[],
  existingNames: ReadonlySet<string>,
): Conflict[] {
  return sources
    .filter((s) => existingNames.has(s.name))
    .map((s) => ({ id: s.id, name: s.name }))
}

/** 一次粘贴的执行计划（逐项）。 */
export interface PastePlanItem {
  id: string
  sourceName: string
  choice: ConflictChoice
  /** `keep-both` 时的新名字；其余情况为空串 */
  finalName: string
}

/**
 * 生成粘贴计划。
 *
 * `choices` 是"每个冲突源项该怎么处理"。没有冲突的项一律 'replace'（语义上是
 * "直接创建"，因为目标处没有同名）。
 *
 * 关键细节：`keep-both` 的新名字必须**在同一次计划内互相避让** —— 一次粘贴
 * 两份同名文件时，如果各自都算 `name (2)`，两份会撞在一起。
 */
export function planPaste(
  sources: readonly { id: string; name: string }[],
  existingNames: ReadonlySet<string>,
  choices: ReadonlyMap<string, ConflictChoice>,
): PastePlanItem[] {
  // 已被占用的名字从"目标现有"开始累积，每确定一个 keep-both 名字就加进去
  const taken = new Set(existingNames)
  const plan: PastePlanItem[] = []

  for (const source of sources) {
    const conflicted = existingNames.has(source.name)
    const choice: ConflictChoice = conflicted ? (choices.get(source.id) ?? 'skip') : 'replace'

    if (choice === 'skip') {
      // 跳过的项不进计划 —— 让调用方能明确知道"这次只做了 N 项"
      continue
    }
    if (choice === 'keep-both') {
      const finalName = uniqueName(source.name, taken)
      taken.add(finalName)
      plan.push({ id: source.id, sourceName: source.name, choice, finalName })
      continue
    }
    // replace：目标处若有同名，最终名字就是原名（后端按名字替换）
    plan.push({ id: source.id, sourceName: source.name, choice, finalName: source.name })
  }
  return plan
}

/**
 * 粘贴是否是**无效操作**：剪切后粘回原目录。
 *
 * 资源管理器在这种情况下会静默不做事（粘贴项置灰）。我们显式返回原因，
 * 好在菜单里给出"剪贴板为空 / 已在原目录"这类可区分的提示 —— 只灰不解释
 * 会让用户反复尝试。
 */
export function pasteBlockedReason(
  payload: ClipboardPayload,
  targetParentId: string,
): 'empty' | 'same-directory' | null {
  if (payload.ids.length === 0) return 'empty'
  // 只有**剪切**在同一目录下才是无效的；复制到原目录会生成副本，是合法操作
  if (payload.mode === 'cut' && payload.fromParentId === targetParentId) return 'same-directory'
  return null
}
