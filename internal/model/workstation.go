package model

// 五大工作台的标识与展示文案。
//
// 唯一事实源是仓库根的 shared/workstations.json：Go 常量（本文件）、前端 TS
// 联合类型（frontend-vue/src/types/workstation.ts）与 Python 枚举
// （python-engine/app/core/capabilities.py 的 WorkstationType）三处声明由
// python-engine/tests/test_workstation_contract.py 比对一致性。
//
// 运行时 Go 侧不读那个 JSON —— 它是比对基准，不是配置源。
//
// 之所以要有这个类型：此前几个标识是散落在 SQL 字面量与前端字符串里的裸值，
// 新增一台时没有任何编译期检查，而 Python 侧的 WorkstationType 早已存在，
// 三端各说各话。
type WorkstationType string

const (
	WorkstationDialogue  WorkstationType = "dialogue"  // 对话工作台（入口）
	WorkstationAgent     WorkstationType = "agent"     // Agent 工作台（智能体）
	WorkstationSkill     WorkstationType = "skill"     // 技能工作台（工具执行）
	WorkstationKnowledge WorkstationType = "knowledge" // 知识库工作台（RAG）
	WorkstationPlugin    WorkstationType = "plugin"    // 插件工作台（扩展）
)

// AllWorkstations 返回全部工作台标识，顺序与 shared/workstations.json 一致。
// 调用方不要缓存返回值后修改它。
func AllWorkstations() []WorkstationType {
	return []WorkstationType{
		WorkstationDialogue,
		WorkstationAgent,
		WorkstationSkill,
		WorkstationKnowledge,
		WorkstationPlugin,
	}
}

// IsValidWorkstation 报告 s 是否为已知工作台标识。
func IsValidWorkstation(s WorkstationType) bool {
	for _, w := range AllWorkstations() {
		if w == s {
			return true
		}
	}
	return false
}
