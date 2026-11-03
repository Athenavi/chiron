/**
 * 中/危操作确认对话框的**文案构造**（纯函数，无 Vue / DOM 依赖）。
 *
 * 为什么文案要单独构造而不是写在模板里：
 *
 * 1. **确认文案的内容决定用户是否真的停下来看**。只写"确定删除吗？"时，用户会
 *    条件反射点确定；写清"将删除 3 个文件夹（含 47 项子内容）与 2 个文件，且无法撤销"
 *    才让人意识到代价。这段逻辑值得被穷举测试，而不是散在模板的三元表达式里。
 * 2. **angd 的 Modal 在测试里同样是懒渲染**（与 Dropdown 一样），组件级断言
 *    "对话框里写了什么"既脆弱又测不到重点。文案层可以纯函数测。
 *
 * 分级原则见 `menuModel.deleteDanger`：按"可恢复成本"分级，不按项数堆砌文字。
 */

import type { DangerLevel } from './menuModel'

/** 一次删除的完整范围（用于生成确认文案）。 */
export interface DeleteScopeDetail {
  /** 直接选中的项数 */
  selectedCount: number
  /** 直接选中的文件夹数量 */
  folderCount: number
  /** 直接选中的文件数量（非文件夹） */
  fileCount: number
  /** 选中文件夹的递归子孙总数（含文件夹与文件）；未知传 null */
  descendantCount: number | null
}

/** 删除确认对话框的渲染描述。 */
export interface DeleteConfirmText {
  titleKey: string
  /** 主文案的 i18n key */
  messageKey: string
  /** 主文案的插值参数 */
  params: Record<string, number>
  /** 危险等级（渲染层据此决定确认按钮是否用危险色） */
  danger: DangerLevel
  /** 是否需要用户主动勾选"我已知晓"才能确认（仅极端情况） */
  requireAcknowledge: boolean
}

/**
 * 生成删除确认文案。
 *
 * 三种形态：
 *  - **plain**：少量纯文件 → 一句话带数量；
 *  - **strong + 已知递归数**：给出精确代价（几个文件夹 / 几个文件 / 共多少项）；
 *  - **strong + 未知递归数**：明确说"含全部子项"，不编造数字。
 *
 * 为什么"未知"要单独一条：编一个数字（比如直接拿 selectedCount）会**低报**代价 ——
 * 用户看到"删除 2 项"以为很小，实际删掉 47 个。宁可说"含全部子项"也不给错数。
 */
export function buildDeleteConfirm(scope: DeleteScopeDetail): DeleteConfirmText {
  const danger: DangerLevel =
    scope.folderCount > 0 || scope.selectedCount > 5 ? 'strong' : 'plain'

  if (danger === 'plain') {
    return {
      titleKey: 'media.delete_confirm_title',
      messageKey: 'media.delete_confirm_plain',
      params: { n: scope.selectedCount },
      danger,
      requireAcknowledge: false,
    }
  }

  // 强确认：能给出精确数字时给数字，给不出就说"含全部子项"
  if (scope.folderCount > 0 && scope.descendantCount !== null) {
    return {
      titleKey: 'media.delete_confirm_title',
      messageKey: 'media.delete_confirm_strong',
      params: {
        folders: scope.folderCount,
        files: scope.fileCount,
      },
      danger,
      // 递归项数很大时要求勾选确认：这是"点错了会真的丢很多东西"的场景
      requireAcknowledge: scope.descendantCount >= 50,
    }
  }

  if (scope.folderCount > 0) {
    return {
      titleKey: 'media.delete_confirm_title',
      messageKey: 'media.delete_confirm_strong_unknown',
      params: {},
      danger,
      requireAcknowledge: false,
    }
  }

  // 没有文件夹但项数多：仍用 plain 文案（一句话带数量足够，不制造额外仪式感）
  return {
    titleKey: 'media.delete_confirm_title',
    messageKey: 'media.delete_confirm_plain',
    params: { n: scope.selectedCount },
    danger,
    requireAcknowledge: false,
  }
}

/** 冲突对话框里一行（一个冲突源项）。 */
export interface ConflictRow {
  id: string
  name: string
  /** 该行当前选中的处置方式 */
  choice: 'replace' | 'skip' | 'keep-both'
  /** 若选 keep-both，将要使用的最终名字（便于用户提前看到结果） */
  previewName: string
}

/** 冲突对话框的渲染描述。 */
export interface ConflictDialogText {
  titleKey: string
  messageKey: string
  params: Record<string, number>
  rows: ConflictRow[]
  /** 是否显示"对全部冲突应用此选择"（只有一个冲突时它是多余的） */
  showApplyAll: boolean
}

/**
 * 生成冲突对话框描述。
 *
 * 关键细节：
 *  - `previewName` 用**同一套** `uniqueName` 规则预先算好，让用户在确认前就能看到
 *    会变成 `a (2).txt` 还是 `a (3).txt` —— 不给预览的话，用户只能赌。
 *  - `showApplyAll` 在只有一个冲突时为 false：单个冲突时那个勾选框只会增加噪音。
 */
export function buildConflictDialog(
  conflicts: readonly { id: string; name: string }[],
  choices: ReadonlyMap<string, 'replace' | 'skip' | 'keep-both'>,
  existingNames: ReadonlySet<string>,
  uniqueNameOf: (name: string, taken: ReadonlySet<string>) => string,
): ConflictDialogText {
  const taken = new Set(existingNames)
  const rows: ConflictRow[] = conflicts.map((c) => {
    const choice = choices.get(c.id) ?? 'skip'
    let previewName = c.name
    if (choice === 'keep-both') {
      // 每个冲突各自算一次，并把结果加进 taken，保证同批多个冲突不会撞在一起
      previewName = uniqueNameOf(c.name, taken)
      taken.add(previewName)
    }
    return { id: c.id, name: c.name, choice, previewName }
  })

  return {
    titleKey: 'media.conflict_title',
    messageKey: 'media.conflict_message',
    params: { n: conflicts.length },
    rows,
    showApplyAll: conflicts.length > 1,
  }
}
