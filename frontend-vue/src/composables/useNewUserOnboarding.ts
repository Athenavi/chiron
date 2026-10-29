/**
 * 新用户引导（P0-3）的**决策层** —— 与浮层分离，这样"该不该弹、哪一步该高亮"
 * 能在 jsdom 里单测（本仓没有浏览器 e2e，能测的必须都在接缝里）。
 *
 * ## 与 `useOnboardingTour` 的分工（别混成一套）
 *
 * - `useOnboardingTour`：**被拦下时**带路（没配模型 / 服务商认证失败），由发送动作触发。
 * - 本模块：**注册后首次进入**的三步引导（看额度 → 选模型 → 发第一条），
 *   状态在服务端 `user_onboarding`，跨设备一致、可用于统计完成率。
 *
 * 两者都基于 antd `Tour`/`Modal`，共用 `TOUR_ANCHORS` 语义锚点，但不是同一个触发条件，
 * 所以不合并 —— 合并会让"被拦下"和"第一次来"共用一份状态，互相干扰。
 */
import { ref } from 'vue'
import {
  completeOnboardingStep,
  getOnboarding,
  type OnboardingStatus,
  type OnboardingStep,
} from '../api/onboarding'

/** 是否应该向这个用户展示引导：只对走过新注册流程、且还没结束的用户弹 */
export function shouldShowGuide(status: OnboardingStatus | null | undefined): boolean {
  if (!status) return false
  // eligible=false ⇒ 存量账号（`user_onboarding` 无行）。不打断已有用户。
  return status.eligible && !status.finished
}

/** 当前该聚焦哪一步：第一个 pending 的步骤；全部结束返回 null */
export function currentStepOf(status: OnboardingStatus | null | undefined): OnboardingStep | null {
  if (!status) return null
  const pending = status.steps.find((s) => s.status === 'pending')
  return pending?.step ?? null
}

/** 某一步的状态（缺省视为 pending，与后端补齐口径一致） */
export function stepStatusOf(
  status: OnboardingStatus | null | undefined,
  step: OnboardingStep,
): 'pending' | 'completed' | 'skipped' {
  return status?.steps.find((s) => s.step === step)?.status ?? 'pending'
}

/**
 * 引导状态 + 提交动作。
 *
 * 所有写操作都**不抛错**：引导是可选的，接口挂了绝不能让聊天页出现报错或空白。
 * 失败只让本地状态停在原处（用户下次进来还会看到），不影响主流程。
 */
export function useNewUserOnboarding() {
  const status = ref<OnboardingStatus | null>(null)
  const open = ref(false)

  async function load(): Promise<void> {
    try {
      status.value = await getOnboarding()
    } catch {
      status.value = null // 拉不到就当没有引导，不阻断聊天
      return
    }
    open.value = shouldShowGuide(status.value)
  }

  /** 首次进入时调用：只在该弹的时候弹 */
  async function maybeOpen(): Promise<boolean> {
    if (open.value) return true
    await load()
    return open.value
  }

  async function mark(step: OnboardingStep, skipped = false): Promise<void> {
    try {
      status.value = await completeOnboardingStep(step, skipped)
    } catch {
      // 静默失败：进度下次再说，主流程不受影响
      return
    }
    // 全部结束（含跳过）后收起，避免用户被一个已经走完的清单挡住
    if (status.value?.finished) open.value = false
  }

  /** 整段跳过：把剩余每一步记成 skipped（跳过 ≠ 完成，完成率不会被灌水） */
  async function skipAll(): Promise<void> {
    const rest = status.value?.steps.filter((s) => s.status === 'pending') ?? []
    for (const s of rest) {
      await mark(s.step, true)
    }
    open.value = false
  }

  function close(): void {
    open.value = false
  }

  return { status, open, load, maybeOpen, mark, skipAll, close }
}

/**
 * 发送成功后调用的便捷函数：把「发出第一轮对话」记成完成。
 *
 * 独立导出的原因：ChatView 只需要一行接入，不必持有整个 composable 实例
 * （那份状态是引导弹窗私有的）。
 */
export async function markFirstMessageDone(): Promise<void> {
  try {
    await completeOnboardingStep('first_message')
  } catch {
    /* 引导进度失败不影响对话 */
  }
}
