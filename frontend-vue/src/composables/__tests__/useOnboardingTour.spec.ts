import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  TOUR_ANCHORS,
  TOUR_SEEN_KEY,
  anchorSelector,
  buildTourSteps,
  hasSeenTour,
  markTourSeen,
  shouldOfferTour,
  tourStepCount,
} from '../useOnboardingTour'
import { PRIVATE_KEY_PREFIXES } from '../../utils/privateStorage'

/**
 * 未配置引导的**决策层**测试（2026-10-09）。
 *
 * 为什么测这一层而不是浮层：本仓**没有浏览器 e2e** ⇒ 光晕位置/RTL 观感属于**手测**，
 * 但"该不该弹、弹几步、弹给谁看"**必须**是自动的（这正是接缝存在的理由，见 §4.2）。
 */

describe('useOnboardingTour（决策层）', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('★★ 没有可用模型 ⇒ 该弹；但"其实已经自动选到了"⇒ **不弹**（那是竞态，弹了就是打扰）', () => {
    expect(shouldOfferTour({ reason: 'no-model', hasModel: false })).toBe(true)
    // 列表还在路上时用户就发了消息 ⇒ ChatView 会自动选第一个 ⇒ 不该打扰
    expect(shouldOfferTour({ reason: 'no-model', hasModel: true })).toBe(false)
    // 认证失败与"有没有模型"无关：模型选得出来，但那个服务商的 key 是坏的
    expect(shouldOfferTour({ reason: 'provider-auth', hasModel: true })).toBe(true)
  })

  it('★ 看过的降级为 1 步（不是"不再提示"）—— 否则第二次被拦下就只剩一句 toast', () => {
    expect(tourStepCount(false)).toBe(3)
    expect(tourStepCount(true)).toBe(1)
  })

  it('★ 步骤数：新用户 3 步 / 看过 1 步 / 认证失败 1 步', () => {
    expect(buildTourSteps({ reason: 'no-model', isAdmin: true, seen: false })).toHaveLength(3)
    expect(buildTourSteps({ reason: 'no-model', isAdmin: true, seen: true })).toHaveLength(1)
    expect(buildTourSteps({ reason: 'provider-auth', isAdmin: true, seen: false })).toHaveLength(1)
  })

  it('★★ 最后一步按角色分支：管理员"去配置" vs 非管理员"联系管理员"（`/models` 需 requiresAdmin）', () => {
    const admin = buildTourSteps({ reason: 'no-model', isAdmin: true, seen: false }).at(-1)
    const member = buildTourSteps({ reason: 'no-model', isAdmin: false, seen: false }).at(-1)

    // 两者文案**必须不同**：非管理员点了也进不去 `/models`，不能诱导跳转
    expect(admin?.description).not.toBe(member?.description)
    expect(String(admin?.nextButtonProps?.children?.())).not.toBe(
      String(member?.nextButtonProps?.children?.()),
    )
  })

  it('★ 锚点用语义属性（不用 CSS 类/结构选择器）', () => {
    expect(anchorSelector('input')).toBe(`[data-tour="${TOUR_ANCHORS.input}"]`)
    expect(anchorSelector('modelPicker')).toContain('data-tour=')
    expect(anchorSelector('modelsAdd')).toContain('data-tour=')
  })

  it('★★ "看过了"按账号隔离（换账号不能继承上一账号的标记）', () => {
    markTourSeen('u-1')
    expect(hasSeenTour('u-1')).toBe(true)
    // 关键：另一个账号必须**不受影响** —— 否则同一浏览器换账号会泄漏"上一账号做过什么"
    expect(hasSeenTour('u-2')).toBe(false)
    // 无账号时退化为 :anonymous（登出态本就不该有标记）
    expect(hasSeenTour(null)).toBe(false)
  })

  it('★★ 该前缀已登记进 PRIVATE_KEY_PREFIXES（否则登出时清不掉 ⇒ 跨账号残留）', () => {
    expect(PRIVATE_KEY_PREFIXES).toContain(TOUR_SEEN_KEY)
  })

  it('存储被禁用时不抛（隐私模式）', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(() => markTourSeen('u-1')).not.toThrow()
    spy.mockRestore()
  })
})
