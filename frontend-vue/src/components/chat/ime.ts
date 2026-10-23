/**
 * CJK 输入法的「伴生 Enter 幽灵键」守卫（从 `ChatInput.vue` 抽出，便于单测）。
 *
 * 现象：中文/日文等拼音输入法下，用户在候选词浮层按 Enter 确认选词时，浏览器会在
 * `compositionend` 之后（同时或数毫秒内）再抛一个 keyCode 13 的伴生 `keydown`。
 * 该伴生事件可能在 `isComposing` 已置为 false **之后**才到达 —— 所以只判
 * `e.isComposing` 会漏，表现为「只想选词，消息却被发出」。
 *
 * 判据：改用 `compositionend` 的时间戳做窗口判断，窗口内的 Enter 一律视为伴生键。
 * 闭环：命中后**必须把时间戳复位为 0**；否则这 500ms 内用户真正的下一次击键会被误吞
 * （参考实现明确点出的"核心闭环"）。复位由本模块给出 `nextTimestamp`，调用方落值。
 */
export const GHOST_ENTER_WINDOW_MS = 500

/**
 * 判断一次 Enter 是否为「选词确认伴生的幽灵键」。
 *
 * @param lastCompositionEndAt `compositionend` 时刻（`performance.now()`）；0 表示还没组合过
 * @param now                  本次 keydown 的时刻（`performance.now()`）
 * @returns `ghost` 为真时要拦截本次 Enter；`nextTimestamp` 是调用方**必须**回写的时间戳
 */
export function classifyEnter(
  lastCompositionEndAt: number,
  now: number,
): { ghost: boolean; nextTimestamp: number } {
  if (lastCompositionEndAt > 0 && now - lastCompositionEndAt < GHOST_ENTER_WINDOW_MS) {
    return { ghost: true, nextTimestamp: 0 }
  }
  return { ghost: false, nextTimestamp: lastCompositionEndAt }
}
