import { describe, expect, it } from 'vitest'
import { collectSkillUsage, usageOf } from '../skillUsage'

describe('collectSkillUsage（反向引用聚合）', () => {
  it('汇总 Agent 侧的 skills 列', () => {
    const usage = collectSkillUsage({
      agents: [{ name: '归档助手', skills: ['weekly-report', 'summarize'] }],
    })
    expect(usageOf(usage, 'weekly-report').agents).toEqual(['归档助手'])
    expect(usageOf(usage, 'summarize').agents).toEqual(['归档助手'])
  })

  it('同名 owner 去重（一个 Agent 重复列同一技能只算一次）', () => {
    const usage = collectSkillUsage({
      agents: [{ name: 'A', skills: ['x', 'x'] }],
    })
    expect(usageOf(usage, 'x').agents).toEqual(['A'])
  })

  it('忽略空技能名与非字符串条目（脏数据不该产出无名条目）', () => {
    const usage = collectSkillUsage({
      agents: [{ name: 'A', skills: ['  ', '', 42, null, 'ok'] }],
    })
    expect([...usage.keys()]).toEqual(['ok'])
  })

  it('skills 缺失或不是数组时跳过该 Agent', () => {
    const usage = collectSkillUsage({
      agents: [{ name: 'A' }, { name: 'B', skills: 'not-an-array' }, { name: 'C', skills: ['s'] }],
    })
    expect(usageOf(usage, 's').agents).toEqual(['C'])
    expect(usage.size).toBe(1)
  })

  it('查不到的技能返回空壳而不是 undefined', () => {
    const entry = usageOf(new Map(), 'nothing')
    expect(entry).toEqual({ agents: [] })
  })
})
