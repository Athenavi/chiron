/**
 * 图片查看器的全局状态。
 *
 * 为什么用模块级状态而不是 props 逐层传：图片散落在消息气泡、markdown 正文、
 * 工具结果里，任何一处都应该能唤起同一个查看器；逐层透传会让每个新增的图片位置
 * 都得记得接一遍事件。查看器本身在 App 上挂一个实例即可。
 *
 * **图集而非单图**（CDS v1 §5.2.2）：一条消息里常有多张图，媒体库更是天然图集。
 * 所以这里是「列表 + 下标」，单图只是长度为 1 的特例 —— 打开接口保持向后兼容，
 * 老调用点（`openImageViewer({ src, alt })`）不用改也不用知道有队列这回事。
 */

import { computed, ref } from 'vue'

export interface ViewerImage {
  src: string
  alt?: string
}

const list = ref<ViewerImage[]>([])
const index = ref(0)

/** 当前图片。用 computed 而不是 ref：换下标与换列表都要能自动同步，避免两份状态打架。 */
const current = computed<ViewerImage | null>(() => list.value[index.value] ?? null)
const hasMultiple = computed(() => list.value.length > 1)
/** 给工具栏的「3 / 7」用（index 从 0 开始，对外从 1 开始）。 */
const position = computed(() => ({ index: index.value, total: list.value.length }))

/** 打开图集。空的 src 会被过滤掉（附件里常有未解析出 URL 的项）。 */
export function openImageViewerList(images: ViewerImage[], startIndex = 0): void {
  const clean = images.filter(img => !!img?.src)
  if (clean.length === 0) return
  list.value = clean
  index.value = Math.min(Math.max(startIndex, 0), clean.length - 1)
}

/** 单图打开：等价于"只有一个元素的图集"。 */
export function openImageViewer(image: ViewerImage): void {
  openImageViewerList([image], 0)
}

export function closeImageViewer(): void {
  list.value = []
  index.value = 0
}

/** 环形切换：最后一张再往右回到第一张（手册的"跑道循环"语义）。
 *  只在有多图时生效 —— 单图下左右键不该有任何副作用。 */
export function nextImage(): void {
  if (list.value.length < 2) return
  index.value = (index.value + 1) % list.value.length
}

export function prevImage(): void {
  if (list.value.length < 2) return
  index.value = (index.value - 1 + list.value.length) % list.value.length
}

export function useImageViewer() {
  return {
    current,
    list,
    index,
    hasMultiple,
    position,
    openImageViewer,
    openImageViewerList,
    closeImageViewer,
    nextImage,
    prevImage,
  }
}
