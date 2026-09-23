import { beforeEach, describe, expect, it } from 'vitest'
import { clearPrivateStorage, privateKey, PRIVATE_KEY_PREFIXES } from '../privateStorage'

// 回归保护：localStorage 是**浏览器级**的。私有数据不带账号命名空间、或登出不清，
// 就会在同一浏览器换账号后被下一个账号读到 —— 实测越权：输入历史 `↑` 召回了他人内容。
describe('privateStorage（私有数据的本地隔离）', () => {
  beforeEach(() => { localStorage.clear() })

  it('privateKey 按账号命名空间隔离，登出态退化为 anonymous', () => {
    expect(privateKey('chat_sessions', 'u1')).toBe('chat_sessions:u1')
    expect(privateKey('chat_sessions', 'u2')).toBe('chat_sessions:u2')
    expect(privateKey('chat_sessions', null)).toBe('chat_sessions:anonymous')
    expect(privateKey('chat_sessions')).toBe('chat_sessions:anonymous')
  })

  it('clearPrivateStorage 清私有键，但不碰 UI 偏好与登录态', () => {
    localStorage.setItem('chat_sessions:u1', '[]')
    localStorage.setItem('chiron:composer-history:v1:u1', '["hi"]')
    // 历史遗留的"无账号命名空间"数据也必须被清掉 —— 它正是越权的来源
    localStorage.setItem('chiron:composer-history:v1', '["legacy"]')
    localStorage.setItem('chiron-theme', 'dark')
    localStorage.setItem('user', '{}')

    clearPrivateStorage()

    expect(localStorage.getItem('chat_sessions:u1')).toBeNull()
    expect(localStorage.getItem('chiron:composer-history:v1:u1')).toBeNull()
    expect(localStorage.getItem('chiron:composer-history:v1')).toBeNull()
    // UI 偏好与登录态不属于"私有数据"，不在此模块职责内
    expect(localStorage.getItem('chiron-theme')).toBe('dark')
    expect(localStorage.getItem('user')).toBe('{}')
  })

  it('清单里的每个前缀都真实生效（防清单与实际用法脱节）', () => {
    expect(PRIVATE_KEY_PREFIXES.length).toBeGreaterThan(0)
    for (const prefix of PRIVATE_KEY_PREFIXES) {
      localStorage.setItem(`${prefix}:u1`, 'x')
      localStorage.setItem(`${prefix}:u2`, 'y')
    }
    clearPrivateStorage()
    for (const prefix of PRIVATE_KEY_PREFIXES) {
      expect(localStorage.getItem(`${prefix}:u1`)).toBeNull()
      expect(localStorage.getItem(`${prefix}:u2`)).toBeNull()
    }
  })
})
