/**
 * 水墨逐字的纯计算（从 `MessageItem.vue` 抽出，便于单测）。
 *
 * 组件里只剩"读 props"与"渲染"，两条硬约束都在这里：
 *  1. 超过 `INK_MAX_CHARS` 不拆 span（逐字 DOM 与字符数线性相关）；
 *  2. `--i` 只算**本批新增**字符的序号，并封顶到 `INK_MAX_STAGGER` ——
 *     否则一次返回一大段时，最后一个字要等十几秒才出现。
 */
export const INK_MAX_CHARS = 2000
export const INK_MAX_STAGGER = 12

/** 流式期可拆分的字符序列；不该拆时返回 null（调用方退回整条插值）。 */
export function inkCharsFor(text: string): string[] | null {
  return text && text.length <= INK_MAX_CHARS ? Array.from(text) : null
}

/** 本批内第 i 个字符的错开档位：0..INK_MAX_STAGGER。 */
export function inkIndexFor(i: number, batchStart: number): number {
  return Math.min(Math.max(i - batchStart, 0), INK_MAX_STAGGER)
}
