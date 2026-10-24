import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ImageViewer from '../common/ImageViewer.vue'
import { closeImageViewer, openImageViewer, openImageViewerList, useImageViewer } from '../common/imageViewerState'

beforeEach(() => { closeImageViewer() })
afterEach(() => {
  // 必须逐个 unmount：查看器在 window 上挂 keydown 监听，不卸载就会累积 ——
  // 同一次按键被 N 个实例各响应一次（方向键会前进 N 步再绕回来）。
  for (const wrapper of mounted.splice(0)) wrapper.unmount()
  closeImageViewer()
})

/** Teleport 会把内容移出组件树，stub 掉它才能用 wrapper.find 断言 */
const mounted: ReturnType<typeof mount>[] = []
const mountViewer = () => {
  const wrapper = mount(ImageViewer, { global: { stubs: { teleport: true } } })
  mounted.push(wrapper)
  return wrapper
}

describe('ImageViewer（图片查看器）', () => {
  it('未打开时不渲染任何东西', () => {
    expect(mountViewer().find('.viewer-mask').exists()).toBe(false)
  })

  it('打开后渲染遮罩、图片与工具条', async () => {
    const wrapper = mountViewer()
    openImageViewer({ src: '/v1/media/a.png', alt: '示意图' })
    await nextTick()

    expect(wrapper.find('.viewer-mask').exists()).toBe(true)
    expect(wrapper.find('.viewer-img').attributes('src')).toBe('/v1/media/a.png')
    expect(wrapper.text()).toContain('示意图')
    expect(wrapper.find('.viewer-percent').text()).toBe('100%')
  })

  it('放大后百分比变化，1:1 恢复原始大小', async () => {
    const wrapper = mountViewer()
    openImageViewer({ src: '/x.png' })
    await nextTick()

    await wrapper.find('[title="放大（+）"]').trigger('click')
    expect(wrapper.find('.viewer-percent').text()).toBe('125%')
    await wrapper.find('[title="原始大小（0）"]').trigger('click')
    expect(wrapper.find('.viewer-percent').text()).toBe('100%')
  })

  it('提供新标签打开与下载链接', async () => {
    const wrapper = mountViewer()
    openImageViewer({ src: '/v1/media/a.png', alt: '示意图' })
    await nextTick()

    const links = wrapper.findAll('a')
    expect(links.map(link => link.attributes('href'))).toEqual(['/v1/media/a.png', '/v1/media/a.png'])
    expect(links[0]!.attributes('target')).toBe('_blank')
    expect(links[0]!.attributes('rel')).toContain('noopener')
    expect(links[1]!.attributes('download')).toBe('示意图')
  })

  it('Esc 关闭，点击遮罩也关闭', async () => {
    const wrapper = mountViewer()
    openImageViewer({ src: '/x.png' })
    await nextTick()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(useImageViewer().current.value).toBeNull()

    openImageViewer({ src: '/x.png' })
    await nextTick()
    await wrapper.find('.viewer-mask').trigger('click')
    await nextTick()
    expect(useImageViewer().current.value).toBeNull()
  })

  it('换图时缩放重置为原始大小', async () => {
    const wrapper = mountViewer()
    openImageViewer({ src: '/a.png' })
    await nextTick()
    await wrapper.find('[title="放大（+）"]').trigger('click')
    expect(wrapper.find('.viewer-percent').text()).toBe('125%')

    openImageViewer({ src: '/b.png' })
    await nextTick()
    expect(wrapper.find('.viewer-percent').text()).toBe('100%')
  })

  it('空 src 不打开（避免露出空遮罩）', async () => {
    mountViewer()
    openImageViewer({ src: '' })
    await nextTick()
    expect(useImageViewer().current.value).toBeNull()
  })

  it('多图时出现索引与左右导航，单图时两者都没有', async () => {
    const wrapper = mountViewer()
    openImageViewerList([{ src: '/a.png' }, { src: '/b.png' }, { src: '/c.png' }], 1)
    await nextTick()

    expect(wrapper.find('.viewer-count').text()).toBe('2 / 3')
    expect(wrapper.find('.viewer-nav.prev').exists()).toBe(true)
    expect(wrapper.find('.viewer-nav.next').exists()).toBe(true)
    expect(wrapper.find('.viewer-img').attributes('src')).toBe('/b.png')

    closeImageViewer()
    openImageViewer({ src: '/only.png' })
    await nextTick()
    // 单图不该露出"1 / 1"和一排点不动的箭头
    expect(wrapper.find('.viewer-count').exists()).toBe(false)
    expect(wrapper.find('.viewer-nav').exists()).toBe(false)
  })

  it('下一个 / 上一个环形切换，且切图时缩放复位', async () => {
    const wrapper = mountViewer()
    openImageViewerList([{ src: '/a.png' }, { src: '/b.png' }], 0)
    await nextTick()

    await wrapper.find('[title="放大（+）"]').trigger('click')
    expect(wrapper.find('.viewer-percent').text()).toBe('125%')

    await wrapper.find('.viewer-nav.next').trigger('click')
    expect(wrapper.find('.viewer-img').attributes('src')).toBe('/b.png')
    expect(wrapper.find('.viewer-count').text()).toBe('2 / 2')
    expect(wrapper.find('.viewer-percent').text()).toBe('100%')

    // 最后一张再往右，回到第一张
    await wrapper.find('.viewer-nav.next').trigger('click')
    expect(wrapper.find('.viewer-img').attributes('src')).toBe('/a.png')
  })

  it('方向键切换图集', async () => {
    const wrapper = mountViewer()
    openImageViewerList([{ src: '/a.png' }, { src: '/b.png' }], 0)
    await nextTick()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }))
    await nextTick()
    expect(wrapper.find('.viewer-img').attributes('src')).toBe('/b.png')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft' }))
    await nextTick()
    expect(wrapper.find('.viewer-img').attributes('src')).toBe('/a.png')
  })

  it('空 src 会被过滤掉，图集下标记到真实项', async () => {
    const wrapper = mountViewer()
    openImageViewerList([{ src: '' }, { src: '/a.png' }, { src: '/b.png' }], 0)
    await nextTick()
    expect(wrapper.find('.viewer-img').attributes('src')).toBe('/a.png')
    expect(wrapper.find('.viewer-count').text()).toBe('1 / 2')
  })
})
