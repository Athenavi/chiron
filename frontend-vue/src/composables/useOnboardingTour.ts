/**
 * 未配置时的引导（antd 自带 `Tour`，**零新依赖**）。
 *
 * ## 为什么需要它
 *
 * 未配置模型时发送会被拦下（`ChatView.ensureModel`）—— 用户**知道出事了**，但不知道
 * **去哪配、点哪里、配完怎么回来**。本模块把那句警告升级成"带路的引导"。
 *
 * ## 为什么用 antd `Tour` 而不是 driver.js（2026-10-09 定）
 *
 * 1. 本仓纪律是**不引新依赖**（`vendor/规划.md` §6）；`ant-design-vue@4.2.6` **自带 `Tour`**
 *    （实测 `es/tour/` 存在）⇒ 同等能力零成本。
 * 2. 更硬的一条：`scripts/check-z-index-tokens.mjs` 只扫 `src/**` 的 `.vue/.css` ⇒
 *    driver.js 这类**运行时注入样式与层级**的库**完全绕过**本仓的 `--z-*` 纪律，
 *    浮层可能盖过 modal/drawer 而且**没有守卫**。antd 的浮层走同一套 zIndex 体系 ✓。
 * 3. RTL（`ar`）、暗色主题、a11y 基础，antd 都已处理；driver.js 要自己接三项。
 *
 * ## 可测性（关键设计）
 *
 * 决策与浮层**分离**：`shouldOfferTour` / `hasSeenTour` / `markTourSeen` 是纯的（可用 jsdom 单测），
 * `useOnboardingTour` 只负责把它们接到 `Tour` 的 `open/steps` 上。本仓**没有浏览器 e2e**
 * ⇒ 光晕位置/RTL 观感属于**手测**（见 doc），但"该不该弹、弹几步"**必须**是自动的。
 */

import { computed, ref } from 'vue'
import { t } from '../i18n'
import { privateKey } from '../utils/privateStorage'

/**
 * 锚点注册表 —— 组件里写 `data-tour="<值>"`，这里按**语义名**取元素。
 *
 * 刻意不用 CSS 类/结构选择器：蹭一次样式重构就断，而且**断了没人知道**（无 e2e）。
 * 语义锚点还能被棘轮对账（见 `docs/development-roadmap.md` 的待办）。
 */
export const TOUR_ANCHORS = {
  input: 'chat-input',
  modelPicker: 'chat-model-picker',
  modelsAdd: 'models-add',
} as const

/** 引导的触发原因 */
export type TourReason = 'no-model' | 'provider-auth'

/** "看过了"标记的键前缀。**必须**同时登记进 `PRIVATE_KEY_PREFIXES`（登出时清）。 */
export const TOUR_SEEN_KEY = 'chiron:chat-setup-tour'

/** 该元素的选择器（供 antd `Tour` 的 `target` 用；取不到时返回 null ⇒ antd 居中显示） */
export function anchorSelector(name: keyof typeof TOUR_ANCHORS): string {
  return `[data-tour="${TOUR_ANCHORS[name]}"]`
}

/**
 * 是否已经看过引导。
 *
 * ⚠ 键**必须**经 `privateKey()` 加账号命名空间：这是"能反推出用户做过什么"的数据，
 * 落在裸 localStorage 会在**换账号**时泄漏给下一个账号（`privateStorage.ts` 开头就是这么写的）。
 */
export function hasSeenTour(userId?: string | null): boolean {
  try {
    return localStorage.getItem(privateKey(TOUR_SEEN_KEY, userId)) === '1'
  } catch {
    return false // 隐私模式 / 存储被禁用：当作没看过，不阻断功能
  }
}

export function markTourSeen(userId?: string | null): void {
  try {
    localStorage.setItem(privateKey(TOUR_SEEN_KEY, userId), '1')
  } catch {
    /* 隐私模式：标记失败不应影响功能 */
  }
}

/**
 * 纯决策：这一刻该不该弹。
 *
 * **唯一"不弹"的情形**是「没有可用模型、但其实已经自动选到了」—— 那是
 * "模型列表还在路上时用户就发了消息"的竞态，`ChatView` 会自动选第一个模型，
 * 此时弹引导属于**打扰** ✓。
 *
 * 注意「看过没有」**不在这里**：看过之后仍然要弹，只是**降级成 1 步**
 * （`tourStepCount`）—— 否则用户第二次被拦下时就只剩一句 toast，等于回到修之前 ✗。
 */
export function shouldOfferTour(opts: { reason: TourReason; hasModel: boolean }): boolean {
  return !(opts.reason === 'no-model' && opts.hasModel)
}

/** 首次给完整 3 步；已经看过则降级为 1 步（"去配置"按钮仍在） */
export function tourStepCount(seen: boolean): 1 | 3 {
  return seen ? 1 : 3
}

/** 尊重 `prefers-reduced-motion`（本仓的动效棘轮只管 token，这条要自己写） */
function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

export interface TourStepLike {
  target: () => HTMLElement | null
  title: string
  description: string
  placement: 'top' | 'bottom' | 'left' | 'right'
  /** antd 的 `TourBtnProps.children` 是**渲染函数**（不是字符串）—— 类型必须对齐 */
  nextButtonProps?: { children: () => string }
}

/**
 * 组装步骤。`isAdmin` 决定最后一步的去向：
 * `/models` 需要 `requiresAdmin`（`router/index.ts:46`）⇒ 普通用户点了也进不去，
 * 所以对非管理员**不诱导跳转**，直接给"请联系管理员"。
 */
export function buildTourSteps(opts: {
  reason: TourReason
  isAdmin: boolean
  seen: boolean
}): TourStepLike[] {
  const byAnchor = (name: keyof typeof TOUR_ANCHORS) => () =>
    document.querySelector<HTMLElement>(anchorSelector(name))

  // 认证失败（reason = provider-auth）：只有一步 —— 同一个服务商的 Key 要修
  if (opts.reason === 'provider-auth') {
    return [
      {
        target: byAnchor('modelPicker'),
        title: t('chat.tour_provider_auth_title'),
        description: t('chat.tour_provider_auth_desc'),
        placement: 'top',
        // ⚠ antd 的 `TourBtnProps.children` 是**渲染函数**，不是字符串
        nextButtonProps: { children: () => (opts.isAdmin ? t('chat.tour_go_configure') : t('common.got_it')) },
      },
    ]
  }

  const first: TourStepLike = {
    target: byAnchor('input'),
    title: t('chat.tour_no_model_title'),
    description: t('chat.tour_no_model_desc'),
    placement: 'top',
  }
  if (tourStepCount(opts.seen) === 1) {
    return [
      {
        ...first,
        nextButtonProps: { children: () => (opts.isAdmin ? t('chat.tour_go_configure') : t('common.got_it')) },
      },
    ]
  }
  return [
    first,
    {
      target: byAnchor('modelPicker'),
      title: t('chat.tour_pick_model_title'),
      description: t('chat.tour_pick_model_desc'),
      placement: 'top',
    },
    {
      target: byAnchor('modelsAdd'),
      title: t('chat.tour_configure_model_title'),
      description: opts.isAdmin ? t('chat.tour_configure_model_desc') : t('chat.tour_ask_admin_desc'),
      placement: 'bottom',
      nextButtonProps: { children: () => (opts.isAdmin ? t('chat.tour_go_configure') : t('common.got_it')) },
    },
  ]
}

/** 把上面这些接到 antd `Tour` 的状态上（组件只用这个 composable，不碰决策） */
export function useOnboardingTour(opts: { userId?: () => string | null | undefined; isAdmin: () => boolean }) {
  const open = ref(false)
  const current = ref(0)
  const seen = ref(false)
  const reason = ref<TourReason>('no-model')
  const count = ref<1 | 3>(3)

  const steps = computed<TourStepLike[]>(() =>
    open.value ? buildTourSteps({ reason: reason.value, isAdmin: opts.isAdmin(), seen: seen.value }) : [],
  )

  /**
   * 尝试引导。返回是否真的弹了 —— 调用方据此决定要不要退化成 toast
   * （**两者互斥**：同一件事不该出现两个浮层）。
   *
   * `hasModel` 由调用方传入（只有它知道当前有没有可用模型）：用于识别那个竞态。
   */
  function offer(r: TourReason, hasModel: boolean): boolean {
    if (!shouldOfferTour({ reason: r, hasModel })) return false
    const already = hasSeenTour(opts.userId?.())
    reason.value = r
    seen.value = already
    count.value = r === 'provider-auth' ? 1 : tourStepCount(already)
    current.value = 0
    open.value = true
    return true
  }

  /** 关闭/完成：标记"看过"并收起（这样下次只降级成一句提示） */
  function finish(): void {
    markTourSeen(opts.userId?.())
    open.value = false
  }

  return {
    open,
    current,
    count,
    reason,
    steps,
    offer,
    finish,
    /** 供组件传给 `Tour` 的 `:animated` */
    animated: computed(() => !prefersReducedMotion()),
  }
}
