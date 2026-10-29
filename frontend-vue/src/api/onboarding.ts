import api from './index'

/**
 * 新用户引导（P0-3）的接口封装。
 *
 * 步骤名与后端 `internal/api/onboarding_handler.go` 的白名单**必须一致** ——
 * 后端对未知 step 返回 400，这里用联合类型把拼写错误挡在编译期。
 */
export type OnboardingStep =
  | 'trial_credit'
  | 'default_model'
  | 'first_message'

export type OnboardingStepStatus = 'pending' | 'completed' | 'skipped'

export interface OnboardingStepState {
  step: OnboardingStep
  status: OnboardingStepStatus
  completed_at?: string
  skipped_at?: string
}

export interface OnboardingStatus {
  /** false = 存量账号（引导表里没有行）⇒ 前端不弹，符合「不打断已有用户」 */
  eligible: boolean
  /** 全部步骤非 pending（完成或跳过）。跳过也算结束 —— 不能把跳过的人永远留在引导里 */
  finished: boolean
  steps: OnboardingStepState[]
  trial: {
    /** 每天免费对话次数（展示用，证明「你已经有权额度」而不是再发一笔） */
    daily_free_limit: number
    /** 固定 true：注册即自带额度，前端不要渲染成「可领取」 */
    already_granted: boolean
    /** 读余额失败时**不出现**该键 —— 此时不显示余额，而不是显示 0 误导用户 */
    credits?: number
  }
}

export async function getOnboarding(): Promise<OnboardingStatus> {
  const { data } = await api.get('/v1/onboarding')
  return data?.data
}

/**
 * 标记一步完成或跳过，返回最新进度。
 *
 * `skipped` 与完成是两种状态：跳过**不算**完成，否则完成率会虚高，
 * 也就看不出用户到底是在哪一步流失的。
 */
export async function completeOnboardingStep(
  step: OnboardingStep,
  skipped = false,
): Promise<OnboardingStatus> {
  const { data } = await api.post('/v1/onboarding/step', { step, skipped })
  return data?.data
}
