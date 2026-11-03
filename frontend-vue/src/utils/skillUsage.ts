/**
 * 统计每个技能被哪些 Agent 在用（反向引用）。
 *
 * 来源：`agents.skills`（JSONB 技能名数组）。引擎派发时 `_get_skills_context`
 * 就是用它筛技能的，因此这里是"谁在用这个技能"的权威处。
 */

export interface SkillUsage {
  agents: string[]
}

export interface SkillUsageSources {
  agents: readonly { name: string; skills?: unknown }[]
}

/**
 * 聚合出 技能名 → { agents }。同名 owner 去重，空技能名忽略。
 */
export function collectSkillUsage(sources: SkillUsageSources): Map<string, SkillUsage> {
  const usage = new Map<string, SkillUsage>()

  for (const agent of sources.agents) {
    if (!Array.isArray(agent.skills)) continue
    for (const skill of agent.skills) {
      if (typeof skill !== 'string') continue
      const name = skill.trim()
      if (!name) continue
      const entry = usage.get(name) ?? { agents: [] }
      if (!entry.agents.includes(agent.name)) entry.agents.push(agent.name)
      usage.set(name, entry)
    }
  }

  return usage
}

/** 查一个技能的使用情况；没有记录时返回空壳，调用方不用再判空 */
export function usageOf(usage: Map<string, SkillUsage>, skillName: string): SkillUsage {
  return usage.get(skillName) ?? { agents: [] }
}
