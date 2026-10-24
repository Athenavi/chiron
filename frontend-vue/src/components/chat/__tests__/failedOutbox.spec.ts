import { describe, it, expect, beforeEach } from 'vitest'
import { addToOutbox, bumpRetryCount, clearOutbox, readOutbox, removeFromOutbox } from '../failedOutbox'

const SID = 's1'
const UID = 'u1'
const KEY = `chiron:outbox:v1:${SID}:${UID}`

beforeEach(() => localStorage.clear())

describe('failedOutbox（未送达消息暂存）', () => {
  it('没有暂存时返回空队列', () => {
    expect(readOutbox(SID, UID)).toEqual([])
  })

  it('入队后按「会话 + 账号」读回，且不越过这两个维度', () => {
    addToOutbox(SID, UID, { content: 'hello' })
    const entries = readOutbox(SID, UID)
    expect(entries).toHaveLength(1)
    expect(entries[0].content).toBe('hello')
    expect(entries[0].retryCount).toBe(0)
    // 隐私边界：换账号 / 换会话都读不到
    expect(readOutbox(SID, 'u2')).toEqual([])
    expect(readOutbox('s2', UID)).toEqual([])
  })

  it('没有 sessionId 时既不写也不读（避免把无会话的失败消息挂到任意会话上）', () => {
    expect(addToOutbox('', UID, { content: 'x' })).toEqual([])
    expect(readOutbox('', UID)).toEqual([])
  })

  it('存储里是坏数据时退化为空，而不是抛错挡住发送流程', () => {
    localStorage.setItem(KEY, '{not json')
    expect(readOutbox(SID, UID)).toEqual([])
    localStorage.setItem(KEY, JSON.stringify({ not: 'an array' }))
    expect(readOutbox(SID, UID)).toEqual([])
  })

  it('重试失败只累加次数，重试成功可逐条移除', () => {
    addToOutbox(SID, UID, { content: 'a' })
    addToOutbox(SID, UID, { content: 'b' })
    const [first] = readOutbox(SID, UID)
    bumpRetryCount(SID, UID, first.id)
    expect(readOutbox(SID, UID)[0].retryCount).toBe(1)
    removeFromOutbox(SID, UID, first.id)
    expect(readOutbox(SID, UID).map(e => e.content)).toEqual(['b'])
  })

  it('清空后把键整个删掉（不留一条空数组）', () => {
    addToOutbox(SID, UID, { content: 'a' })
    clearOutbox(SID, UID)
    expect(localStorage.getItem(KEY)).toBeNull()
    expect(readOutbox(SID, UID)).toEqual([])
  })
})
