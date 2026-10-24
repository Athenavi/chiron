/**
 * 灯箱共享元素形变（CDS v1 §5.2.2）。
 *
 * 用原生 View Transitions API：给**缩略图**与**全屏图**挂同一个
 * `view-transition-name`，浏览器自己把两者的位置/尺寸/圆角补间成"从缩略图长到全屏、
 * 再原路飞回"。比手写 FLIP 少一套测量代码，也不会和 Vue 的 patch 抢帧。
 *
 * 三条硬约束，写在这里免得后来者踩：
 *  1. **同一时刻只能有一个元素持有这个名字** —— 两个同名元素会让浏览器直接忽略形变。
 *     所以打开时"先给缩略图挂名 → 转场 → 新状态里摘掉缩略图"；关闭时反过来。
 *  2. **降级优先**：不支持 `startViewTransition` 的引擎（Safari 18 以下、jsdom）直接
 *     走原路径 —— 动画绝不能成为"打不开图片"的原因。
 *  3. **转场可能被连点打断**：`finished` 会 reject，必须 catch，否则控制台一片红。
 */
import { nextTick } from 'vue'
import { closeImageViewer, openImageViewerList, type ViewerImage } from './imageViewerState'

export const LIGHTBOX_VT_NAME = 'cds-lightbox'

type ViewTransitionDocument = Document & {
  startViewTransition?: (cb: () => void | Promise<void>) => { finished: Promise<void> }
}

function canAnimate(): boolean {
  return typeof document !== 'undefined'
    && typeof (document as ViewTransitionDocument).startViewTransition === 'function'
}

/** 打开灯箱时的那个缩略图元素：关闭时要把它还回去，形变才能原路飞回。 */
let lastThumb: HTMLElement | null = null

function setName(el: HTMLElement | null, name: string): void {
  if (el) el.style.viewTransitionName = name
}

async function transition(run: () => void | Promise<void>): Promise<void> {
  if (!canAnimate()) {
    await run()
    return
  }
  try {
    await (document as ViewTransitionDocument).startViewTransition!(run)?.finished
  } catch {
    /* 被连点或组件卸载打断：功能已经生效，动画没有就没有 */
  }
}

/** 从缩略图打开图集。`thumb` 可为空（外部链接等场景），此时退化为普通打开。 */
export async function openLightboxFromList(
  thumb: HTMLElement | null,
  images: ViewerImage[],
  startIndex = 0,
): Promise<void> {
  lastThumb = thumb
  // 旧状态里必须已经有名字，捕获才认得"从哪儿长出来"
  setName(thumb, LIGHTBOX_VT_NAME)
  await transition(async () => {
    openImageViewerList(images, startIndex)
    // 新状态里只留全屏图持有这个名字，否则同名冲突 → 形变被忽略
    setName(thumb, '')
    await nextTick()
  })
}

/** 单图打开：图集长度为 1 的便捷包装。 */
export async function openLightboxFrom(
  thumb: HTMLElement | null,
  payload: ViewerImage,
): Promise<void> {
  await openLightboxFromList(thumb, [payload], 0)
}

/** 关闭灯箱：把名字还给缩略图，转场结束后摘掉（它还留在对话流里）。 */
export async function closeLightbox(): Promise<void> {
  const thumb = lastThumb
  await transition(async () => {
    closeImageViewer()
    setName(thumb, LIGHTBOX_VT_NAME)
    await nextTick()
  })
  setName(thumb, '')
  lastThumb = null
}
