import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

/**
 * `utils/uploader.ts` 的测试（2026-10-09）—— 此前零测试 ✗。
 *
 * 第 149 轮把无测试模块分成"有人用 / 没人用"两堆 ✓，本轮从**有人用**里挑这个（143 行 · 3 处引用）——
 * 它是**分片上传 + 断点续传**，真逻辑最多 ✓，而且**注释自己写明了一个已修的数据损坏 bug**（`:53-56`）✓。
 *
 * **钉四条不变量**：
 * ① **文件指纹必须能区分"同名同大小"的文件** ✓✓（注释：只用 name+size 会让导出文件/重复下载/
 *    批量截图**复用同一个 upload_id**，把 A 的分片写进 B 的上传会话）⇒ 指纹含 `lastModified` 与分片大小 ✓；
 * ② **断点续传要跳过服务端已收的分片**（`:94`）—— 否则续传等于重传，白费带宽 ✓；
 * ③ **失败重试 3 次、指数退避**（`:98-110`）—— 前两次失败不该让整个上传失败 ✓；
 * ④ **重试耗尽后 `done` 必须 reject**（`:107`）—— 不能"静默成功" ✓。
 */

const apiMocks = vi.hoisted(() => ({
  // 形参要写出来：否则 `vi.fn(() => …)` 的 calls 被推断成 `[]`，`mock.calls[0][0]` 会报 TS2493。
  // 而本仓**没开** `argsIgnorePattern`（见 ChatView.spec.ts 的同类说明）⇒ 形参一律显式 `void`，
  // 否则 lint 会因为"未使用的参数"多出警告。
  put: vi.fn((url?: string, body?: unknown, cfg?: unknown): Promise<unknown> => {
    void url; void body; void cfg
    return Promise.resolve({ data: {} })
  }),
  get: vi.fn((url?: string): Promise<unknown> => {
    void url
    return Promise.resolve({ data: { data: { received_chunks: [] } } })
  }),
  post: vi.fn((url?: string, body?: unknown): Promise<unknown> => {
    void url; void body
    return Promise.resolve({ data: { data: { upload_id: 'u-1' } } })
  }),
}))

vi.mock('../../api', () => ({ api: apiMocks }))

import { createChunkUpload } from '../uploader'

/** 造一个可 slice 的 File（jsdom 的 File 支持 slice ✓） */
function makeFile(name: string, size: number, lastModified: number): File {
  const f = new File([new Uint8Array(size)], name, { lastModified })
  return f
}

const baseOpts = { purpose: 'generic' as const, chunkSize: 100, concurrency: 2 }

describe('utils/uploader（分片上传 / 断点续传）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    apiMocks.get.mockResolvedValue({ data: { data: { received_chunks: [] } } })
    apiMocks.post.mockImplementation((url?: string) => {
      if (url === '/v1/uploads') return Promise.resolve({ data: { data: { upload_id: 'u-1' } } })
      return Promise.resolve({ data: { data: { file_url: '/f', upload_id: 'u-1', size: 250 } } })
    })
    apiMocks.put.mockResolvedValue({ data: {} })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('★ 同名同大小的两个文件**不能**共用 upload_id（指纹含 lastModified）', async () => {
    // 同名、同大小、不同 lastModified —— 导出文件/重复下载的典型场景
    const a = makeFile('export.csv', 250, 1_700_000_000_000)
    const b = makeFile('export.csv', 250, 1_700_000_999_000)

    const ha = await createChunkUpload(a, baseOpts)
    await ha.done
    const hb = await createChunkUpload(b, baseOpts)
    await hb.done

    // 两次都要**新建**上传会话（若指纹漏了 lastModified，第二次会复用 u-1 而不再 POST）
    expect(apiMocks.post).toHaveBeenCalledTimes(4)   // 2 次 create + 2 次 complete
    const createdIds = apiMocks.post.mock.calls.filter(c => c[0] === '/v1/uploads')
    expect(createdIds).toHaveLength(2)
  })

  it('★ 断点续传：服务端已收的分片不再重传', async () => {
    apiMocks.post.mockImplementation((url?: string) => {
      if (url === '/v1/uploads') return Promise.resolve({ data: { data: { upload_id: 'u-1' } } })
      return Promise.resolve({ data: { data: { file_url: '/f', upload_id: 'u-1', size: 250 } } })
    })
    // 预置：这个文件已经建过会话，且服务端已收 0 与 1
    const f = makeFile('a.bin', 250, 111)
    const fingerprint = `chunk-upload:generic::a.bin:250:111:100`
    localStorage.setItem(fingerprint, 'u-1')
    apiMocks.get.mockResolvedValue({ data: { data: { received_chunks: [0, 1] } } })

    const h = await createChunkUpload(f, baseOpts)
    await h.done

    // 共 3 片（250 / 100），已收 2 片 ⇒ 只该 PUT 一次
    expect(apiMocks.put).toHaveBeenCalledTimes(1)
    expect(apiMocks.put.mock.calls[0]?.[0]).toBe('/v1/uploads/u-1/chunks/2')
    // 续传时**不该**再 create
    expect(apiMocks.post.mock.calls.filter(c => c[0] === '/v1/uploads')).toHaveLength(0)
  })

  it('★ 单片的失败会重试（前两次失败不该让整个上传失败）', async () => {
    vi.useFakeTimers()
    let calls = 0
    apiMocks.put.mockImplementation(() => {
      calls++
      if (calls <= 2) return Promise.reject(new Error('network'))
      return Promise.resolve({ data: {} })
    })

    const f = makeFile('small.bin', 50, 1)   // 单片
    const h = await createChunkUpload(f, { ...baseOpts, concurrency: 1 })

    // 让退避的定时器跑完
    const p = h.done
    await vi.advanceTimersByTimeAsync(5000)
    await expect(p).resolves.toBeTruthy()
    expect(calls).toBe(3)                    // 两次失败 + 第三次成功
  })

  it('★ 重试耗尽后 done 必须 reject（不能静默成功）', async () => {
    vi.useFakeTimers()
    apiMocks.put.mockRejectedValue(new Error('always down'))

    const f = makeFile('small.bin', 50, 1)
    const h = await createChunkUpload(f, { ...baseOpts, concurrency: 1 })

    const p = h.done
    const guarded = p.catch((e: unknown) => e)   // 先挂上，避免 unhandled rejection
    await vi.advanceTimersByTimeAsync(5000)
    await expect(guarded).resolves.toBeInstanceOf(Error)
    // 重试 3 次（首次 + 2 次重试）
    expect(apiMocks.put.mock.calls.length).toBe(3)
    // 失败**不该**调 complete，也不该清掉断点记录
    expect(apiMocks.post.mock.calls.filter(c => String(c[0]).includes('/complete'))).toHaveLength(0)
  })
})
