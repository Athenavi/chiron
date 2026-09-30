import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import { currentProject, projectParams, setProject } from '../useProject'

const STORAGE_KEY = 'chiron.memory.project'

describe('useProject（C3：当前项目）', () => {
  beforeEach(() => {
    localStorage.clear()
    setProject('')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('缺省是"未分组"（空串），请求不带 project 参数', () => {
    expect(currentProject.value).toBe('')
    expect(projectParams()).toEqual({})
  })

  it('切换项目后持久化，并进入请求参数', () => {
    setProject('proj-a')

    expect(currentProject.value).toBe('proj-a')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('proj-a')
    expect(projectParams()).toEqual({ project: 'proj-a' })
  })

  it('清空 = 回到未分组，并移除持久化键', () => {
    setProject('proj-a')
    setProject('')

    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
    expect(projectParams()).toEqual({})
  })

  it('两端空白被裁掉（否则"看着一样的项目"会变成两个）', () => {
    setProject('  proj-a  ')

    expect(currentProject.value).toBe('proj-a')
  })

  it('localStorage 不可用时仍能在当前会话内生效', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded')
    })

    setProject('proj-b')

    expect(currentProject.value).toBe('proj-b')
  })
})
