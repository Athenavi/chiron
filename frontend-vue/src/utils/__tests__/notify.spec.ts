import { describe, expect, it, vi } from 'vitest'

// vi.mock 会被提升到文件顶部，因此 mock 对象必须用 vi.hoisted 创建。
const { message } = vi.hoisted(() => ({
  message: {
    success: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
    error: vi.fn(),
  },
}))
vi.mock('ant-design-vue', () => ({ message }))

import { NOTIFY_ERROR_DURATION, NOTIFY_OK_DURATION, notify } from '../notify'

describe('notify 时长（§13 已定参数）', () => {
  it('成功 / 提醒 / 信息：2.5s', () => {
    notify.success('ok')
    notify.warning('warn')
    notify.info('info')

    expect(NOTIFY_OK_DURATION).toBe(2.5)
    for (const fn of [message.success, message.warning, message.info]) {
      expect(fn).toHaveBeenCalledWith({ content: expect.any(String), duration: 2.5 })
    }
  })

  it('错误：4s（需要读完再消失）', () => {
    notify.error('boom')

    expect(NOTIFY_ERROR_DURATION).toBe(4)
    expect(message.error).toHaveBeenCalledWith({ content: 'boom', duration: 4 })
  })
})
