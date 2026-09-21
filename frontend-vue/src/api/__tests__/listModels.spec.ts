import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, listModels } from '../index'

/**
 * `listModels` 是对话页模型下拉的唯一数据源。
 *
 * 回归背景（真实故障）：后端 `/v1/models` 走 `OK()` 包装
 * （`{success, data:{models:[…]}}`），而这里曾写成 `resp.data?.models` ——
 * **少了一层 `.data`** → `undefined` → 静默返回空数组 → 下拉永远显示"暂无数据"。
 * 之所以长期没暴露：当时后端恰好也是空的，两种 bug 的症状完全一致、互相掩盖。
 */
describe('listModels（对话页模型下拉的数据源）', () => {
  afterEach(() => vi.restoreAllMocks())

  it('解析 OK() 包装形态 {success, data:{models}}', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({
      data: {
        success: true,
        data: {
          models: [
            { provider: 'opencode-go', name: 'glm-5', display_name: 'glm-5', context_window: 128000 },
            { provider: 'opencode-go', name: 'deepseek-v4-pro', display_name: '', context_window: 128000 },
          ],
        },
      },
    } as never)

    const models = await listModels()
    expect(models).toHaveLength(2)
    expect(models[0]!.name).toBe('glm-5')
  })

  it('兼容未包装的裸形态 {models}', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({
      data: { models: [{ provider: 'p', name: 'm', display_name: '', context_window: 8192 }] },
    } as never)

    expect(await listModels()).toHaveLength(1)
  })

  it('取不到 models 时返回空数组（不抛错、不返回 undefined）', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ data: { success: true, data: {} } } as never)
    expect(await listModels()).toEqual([])
  })

  it('models 不是数组时也返回空数组（防御后端异常形态）', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ data: { success: true, data: { models: null } } } as never)
    expect(await listModels()).toEqual([])
  })
})
