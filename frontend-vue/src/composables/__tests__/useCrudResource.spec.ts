import { describe, it, expect, vi } from 'vitest'
import { apiErrorMessage, useCrudResource } from '../useCrudResource'

/**
 * `composables/useCrudResource.ts` 的测试（2026-10-09）—— 此前零测试 ✗。
 *
 * 第 149 轮的批量量法把这 19 个无测试模块分成"有人用 / 没人用"两堆 ✓，本轮从**有人用**那堆里挑
 * **被引用最多**的一个（**4 处**：`DatabaseManagementView` 等）—— 共享抽象的杠杆最大 ✓。
 *
 * **行为契约写在它自己的注释里**（`:18-21`），用例照抄：
 * ① `loading` **初始为 true**、`load` 结束置 false（视图不该先闪一下空态 ✓）；
 * ② **加载失败时保留上一次的 data** ✓✓ —— 只记 `error`，**不把已有数据清掉** ✓
 *    （这是最强的一条：刷新失败不该让用户眼前的东西消失 ✓）；
 * ③ loader 是闭包、`reload` 就是再调一次 `load` ✓。
 *
 * **⚠ 一处文档与代码不符**（顺手记下）：注释说"仅记录 error 并 **console.error**"，
 * 但 `:33-37` 的 `catch` **只设 `error`**，并没有 `console.error` ✓ —— 以代码为准 ✓（不影响行为 ✓）。
 */

describe('useCrudResource', () => {
  it('★ 初始 loading 为 true，data 为传入的初值（视图不闪空态）', () => {
    const { data, loading, error } = useCrudResource<string[]>([], async () => [])
    expect(loading.value).toBe(true)
    expect(data.value).toEqual([])
    expect(error.value).toBeNull()
  })

  it('★ 成功：data 换成 loader 的结果，loading 落下、error 为 null', async () => {
    const { data, loading, error, load } = useCrudResource<string[]>([], async () => ['a', 'b'])
    await load()

    expect(data.value).toEqual(['a', 'b'])
    expect(loading.value).toBe(false)
    expect(error.value).toBeNull()
  })

  it('★★ 失败：保留上一次的 data（不清空），只记 error；loading 仍要落下', async () => {
    let ok = true
    const loader = async () => {
      if (!ok) throw { response: { data: { error: '后端说了原因' } } }
      return ['已有的数据']
    }
    const { data, loading, error, load } = useCrudResource<string[]>([], loader)

    await load()
    expect(data.value).toEqual(['已有的数据'])

    // 第二次失败：数据**必须还在**
    ok = false
    await load()
    expect(data.value).toEqual(['已有的数据'])   // ← 关键：没被清空
    expect(error.value).toBe('后端说了原因')
    expect(loading.value).toBe(false)            // finally 里落下，不会永远转圈
  })

  it('★ 失败且后端没给原因时，回落到兜底文案', async () => {
    const { error, load } = useCrudResource<null>(null, async () => {
      throw new Error('boom')
    })
    await load()
    expect(typeof error.value).toBe('string')
    expect(error.value!.length).toBeGreaterThan(0)
  })

  it('reload 就是再调一次 load（闭包可读视图状态）', async () => {
    let page = 1
    const loader = vi.fn(async () => [`page-${page}`])
    const { data, load, reload } = useCrudResource<string[]>([], loader)

    await load()
    expect(data.value).toEqual(['page-1'])

    page = 2                      // 视图改了筛选/分页状态
    await reload()
    expect(data.value).toEqual(['page-2'])
    expect(loader).toHaveBeenCalledTimes(2)
  })
})

describe('apiErrorMessage', () => {
  it('★ 取出 error.response.data.error；取不到就用兜底', () => {
    expect(apiErrorMessage({ response: { data: { error: '具体原因' } } }, '兜底')).toBe('具体原因')

    // 各种"取不到"的形状都要回落，且不能抛
    expect(apiErrorMessage(null, '兜底')).toBe('兜底')
    expect(apiErrorMessage(undefined, '兜底')).toBe('兜底')
    expect(apiErrorMessage({}, '兜底')).toBe('兜底')
    expect(apiErrorMessage({ response: {} }, '兜底')).toBe('兜底')
    expect(apiErrorMessage({ response: { data: { error: '' } } }, '兜底')).toBe('兜底')
    expect(apiErrorMessage({ response: { data: { error: 42 } } }, '兜底')).toBe('兜底')
  })
})
