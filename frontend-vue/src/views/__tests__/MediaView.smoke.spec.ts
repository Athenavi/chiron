import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 媒体库页的冒烟网（抽 composable 前置）。
 *
 * 为什么先补这几条：`MediaView.vue` 是唯一「1800+ 行且零测试」的页面。
 * 在这种页面上直接抽 composable 等于盲改 —— 搬错一个 `ref` 的归属，
 * 界面会静默显示旧值而不报错。这五条钉住的是抽取时最容易搬坏的四件事：
 * 请求参数、列表/空态/错误态三态互斥、进入目录时的 parent_id、点击文件不重新拉列表。
 */

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
  resolveMediaUrl: vi.fn(async (a: { id?: string; file_url?: string }) => a?.file_url || ''),
}))

// antd 的 Table / Segmented 走 responsiveObserve，jsdom 没有 matchMedia。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

vi.mock('../../api', () => ({
  api: {
    get: apiMocks.get,
    post: apiMocks.post,
    put: apiMocks.put,
    delete: apiMocks.delete,
  },
  resolveMediaUrl: apiMocks.resolveMediaUrl,
}))

vi.mock('../../utils/uploader', () => ({ createChunkUpload: vi.fn() }))
vi.mock('@file-viewer/vue3', () => ({
  FileViewer: { name: 'FileViewer', template: '<div class="file-viewer-stub" />' },
}))
vi.mock('@file-viewer/preset-all', () => ({ default: {} }))

import MediaView from '../MediaView.vue'
import PageSkeleton from '../../components/common/PageSkeleton.vue'
import EmptyState from '../../components/common/EmptyState.vue'
import { i18n } from '../../i18n'

const tr = (key: string): string => (i18n.global as unknown as { t: (k: string) => string }).t(key)

function listResponse(items: unknown[], total = items.length) {
  return { data: { data: { items, total } } }
}

const folder = {
  id: 'f1',
  type: 'folder',
  name: '归档',
  size: 0,
  file_url: '',
  created_at: '2026-01-01T00:00:00Z',
}

const image = {
  id: 'm1',
  type: 'image',
  name: 'a.png',
  size: 1234,
  file_url: '/media/a.png',
  mime_type: 'image/png',
  created_at: '2026-01-02T00:00:00Z',
}

async function mountView() {
  const wrapper = mount(MediaView)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('MediaView 加载与三态', () => {
  it('挂载后以默认参数拉取第一页', async () => {
    apiMocks.get.mockResolvedValue(listResponse([]))
    await mountView()

    expect(apiMocks.get).toHaveBeenCalledTimes(1)
    expect(apiMocks.get).toHaveBeenCalledWith('/v1/media', {
      params: { page: 1, page_size: 50, parent_id: '' },
    })
  })

  it('有数据时渲染卡片，且骨架与空态都不出现', async () => {
    apiMocks.get.mockResolvedValue(listResponse([folder, image]))
    const wrapper = await mountView()

    expect(wrapper.findAll('.file-card')).toHaveLength(2)
    expect(wrapper.findComponent(PageSkeleton).exists()).toBe(false)
    expect(wrapper.findComponent(EmptyState).exists()).toBe(false)
  })

  it('返回空列表时显示空态而不是错误态', async () => {
    apiMocks.get.mockResolvedValue(listResponse([]))
    const wrapper = await mountView()

    expect(wrapper.findAll('.file-card')).toHaveLength(0)
    expect(wrapper.findComponent(EmptyState).props('description')).toBe(tr('media.no_files_yet'))
  })

  it('请求失败时显示错误态，而不是把失败误当成"没有文件"', async () => {
    apiMocks.get.mockRejectedValue(new Error('boom'))
    const wrapper = await mountView()

    expect(wrapper.findAll('.file-card')).toHaveLength(0)
    expect(wrapper.findComponent(EmptyState).props('description')).toBe(tr('errors.failed_to_load'))
  })
})

describe('MediaView 目录导航', () => {
  it('点击文件夹卡片进入该目录，并把 folder id 作为 parent_id 重新拉取', async () => {
    apiMocks.get.mockResolvedValueOnce(listResponse([folder, image]))
    const wrapper = await mountView()
    expect(apiMocks.get).toHaveBeenCalledTimes(1)

    apiMocks.get.mockResolvedValueOnce(listResponse([]))
    await wrapper.findAll('.file-card')[0].trigger('click')
    await flushPromises()

    expect(apiMocks.get).toHaveBeenCalledTimes(2)
    expect(apiMocks.get.mock.calls[1][1]).toEqual({
      params: { page: 1, page_size: 50, parent_id: 'f1' },
    })
  })

  it('点击文件卡片只打开详情，不重新拉列表', async () => {
    apiMocks.get.mockResolvedValueOnce(listResponse([folder, image]))
    const wrapper = await mountView()

    await wrapper.findAll('.file-card')[1].trigger('click')
    await flushPromises()

    expect(apiMocks.get).toHaveBeenCalledTimes(1)
  })
})
