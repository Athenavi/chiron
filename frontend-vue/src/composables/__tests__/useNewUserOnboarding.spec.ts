import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * 新用户引导（P0-3）的决策层测试。
 *
 * 本仓没有浏览器 e2e ⇒ "该不该弹、哪一步待办、跳过是否算完成"必须在这里钉住。
 * 接口层用 mock：这里测的是**判定与状态机**，不是 HTTP。
 */
vi.mock('../../api/onboarding', () => ({
  getOnboarding: vi.fn(),
  completeOnboardingStep: vi.fn(),
}))

import { getOnboarding, completeOnboardingStep } from '../../api/onboarding'
import type { OnboardingStatus } from '../../api/onboarding'
import {
  shouldShowGuide,
  currentStepOf,
  stepStatusOf,
  useNewUserOnboarding,
} from '../useNewUserOnboarding'

function status(overrides: Partial<OnboardingStatus> = {}): OnboardingStatus {
  return {
    eligible: true,
    finished: false,
    steps: [
      { step: 'trial_credit', status: 'completed' },
      { step: 'default_model', status: 'pending' },
      { step: 'first_message', status: 'pending' },
    ],
    trial: { daily_free_limit: 5, already_granted: true },
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('shouldShowGuide', () => {
  it('对走过新注册流程且未结束的用户弹', () => {
    expect(shouldShowGuide(status())).toBe(true)
  })

  it('存量账号（eligible=false）不弹', () => {
    // 没有 eligible 判定的话，所有老用户都会在登录后被打扰一次
    expect(shouldShowGuide(status({ eligible: false }))).toBe(false)
  })

  it('已经走完（含全部跳过）不弹', () => {
    expect(shouldShowGuide(status({ finished: true }))).toBe(false)
  })

  it('拉不到状态时不弹，也不报错', () => {
    expect(shouldShowGuide(null)).toBe(false)
    expect(shouldShowGuide(undefined)).toBe(false)
  })
})

describe('currentStepOf / stepStatusOf', () => {
  it('返回第一个 pending 的步骤', () => {
    expect(currentStepOf(status())).toBe('default_model')
  })

  it('全部结束返回 null', () => {
    const done = status({
      finished: true,
      steps: [
        { step: 'trial_credit', status: 'completed' },
        { step: 'default_model', status: 'skipped' },
        { step: 'first_message', status: 'completed' },
      ],
    })
    expect(currentStepOf(done)).toBeNull()
  })

  it('缺失的步骤按 pending 处理（与后端补齐口径一致）', () => {
    expect(stepStatusOf({ ...status(), steps: [] }, 'first_message')).toBe('pending')
  })
})

describe('useNewUserOnboarding', () => {
  it('load 后按判定自动打开', async () => {
    vi.mocked(getOnboarding).mockResolvedValue(status())
    const o = useNewUserOnboarding()
    await o.load()
    expect(o.open.value).toBe(true)
  })

  it('接口失败时静默——不抛错、不弹', async () => {
    vi.mocked(getOnboarding).mockRejectedValue(new Error('boom'))
    const o = useNewUserOnboarding()
    await expect(o.load()).resolves.toBeUndefined()
    expect(o.open.value).toBe(false)
    expect(o.status.value).toBeNull()
  })

  it('完成的步骤不在未结束时收起浮层', async () => {
    vi.mocked(getOnboarding).mockResolvedValue(status())
    vi.mocked(completeOnboardingStep).mockResolvedValue(
      status({
        steps: [
          { step: 'trial_credit', status: 'completed' },
          { step: 'default_model', status: 'completed' },
          { step: 'first_message', status: 'pending' },
        ],
      }),
    )
    const o = useNewUserOnboarding()
    await o.load()
    await o.mark('default_model')
    expect(o.open.value).toBe(true)
  })

  it('最后一步完成后收起浮层', async () => {
    vi.mocked(getOnboarding).mockResolvedValue(status())
    vi.mocked(completeOnboardingStep).mockResolvedValue(status({ finished: true }))
    const o = useNewUserOnboarding()
    await o.load()
    await o.mark('first_message')
    expect(o.open.value).toBe(false)
  })

  it('写接口失败不打断主流程', async () => {
    vi.mocked(getOnboarding).mockResolvedValue(status())
    vi.mocked(completeOnboardingStep).mockRejectedValue(new Error('offline'))
    const o = useNewUserOnboarding()
    await o.load()
    await expect(o.mark('default_model')).resolves.toBeUndefined()
    // 状态停在原处：用户下次进来还会看到，而不是被当成已完成
    expect(o.status.value?.finished).toBe(false)
  })

  it('整段跳过把剩余每步记成 skipped（不是 completed）', async () => {
    vi.mocked(getOnboarding).mockResolvedValue(status())
    vi.mocked(completeOnboardingStep).mockResolvedValue(status({ finished: true }))
    const o = useNewUserOnboarding()
    await o.load()
    await o.skipAll()

    // 已完成的 trial_credit 不该被再写一次；跳过 ≠ 完成，否则完成率会虚高
    const calls = vi.mocked(completeOnboardingStep).mock.calls
    expect(calls.map((c) => c[0])).toEqual(['default_model', 'first_message'])
    expect(calls.every((c) => c[1] === true)).toBe(true)
    expect(o.open.value).toBe(false)
  })
})
