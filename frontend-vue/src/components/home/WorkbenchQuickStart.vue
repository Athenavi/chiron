<script setup lang="ts">
/**
 * 首页的协作入口：把知识库 / Agent / 技能 / 工作流"带着"一起进入对话。
 *
 * 之前首页的六张卡片是六个互不相干的跳转按钮 —— 进去之后的组合能力
 * （对话页的 context chips）用户根本不知道存在。这里把"带能力进对话"
 * 提到最外层，并且选项直接来自各工作台的真实数据。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Select } from 'ant-design-vue'
import {
  listAgents,
  listKnowledgeBases,
  listPlugins,
  listSkillResources,
  listWorkflows,
  type WorkbenchResource,
} from '../../api'
import { buildContextQuery, type ContextChip, type ContextChipType } from '../chat/contextChips'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
interface Source {
  type: ContextChipType
  label: string
  placeholder: string
  load: () => Promise<WorkbenchResource[]>
}

const router = useRouter()

const sources: Source[] = [
  { type: 'kb', label: t('知识库'), placeholder: t('检索增强'), load: listKnowledgeBases },
  {
    type: 'agent',
    label: 'Agent',
    placeholder: t('指定执行者'),
    load: async () => (await listAgents()).map(a => ({ id: a.id, name: a.name })),
  },
  { type: 'skill', label: t('技能'), placeholder: t('装配技能'), load: listSkillResources },
  { type: 'workflow', label: t('工作流'), placeholder: t('按编排执行'), load: listWorkflows },
  { type: 'plugin', label: t('插件'), placeholder: t('限定工具集'), load: listPlugins },
  // 长期记忆分类（问题 3）：此前记忆是唯一**没有 UI 构造入口**的上下文类型 ——
  // 用户无法从首页"带记忆进对话"，只能手写 URL 的 ?memory=xxx。
  { type: 'memory', label: t('记忆'), placeholder: t('注入长期记忆'), load: async () => MEMORY_SLOT_OPTIONS },
]

/**
 * 长期记忆分类，与后端 python-engine/app/memory/layers.py 的 SLOT_LABELS 一一对应。
 * 服务端语义（workbench_context.selected_memory_slots）：**不选 = 注入全部**；
 * 选了则只注入这些分类；选「全部记忆」(all) 与不选等价。
 */
const MEMORY_SLOT_OPTIONS: WorkbenchResource[] = [
  { id: 'all', name: t('全部记忆') },
  { id: 'identity', name: t('身份') },
  { id: 'preference', name: t('偏好') },
  { id: 'decision', name: t('关键决策') },
  { id: 'fact', name: t('长期事实') },
]

const emptyOptions = (): Record<ContextChipType, WorkbenchResource[]> => ({ kb: [], agent: [], skill: [], workflow: [], plugin: [], memory: [] })
const options = ref<Record<ContextChipType, WorkbenchResource[]>>(emptyOptions())
const picked = ref<Record<ContextChipType, string[]>>({ kb: [], agent: [], skill: [], workflow: [], plugin: [], memory: [] })
const loading = ref(true)

onMounted(async () => {
  // 各工作台接口互相独立：任一失败只让那一类为空，不拖垮整个入口
  const results = await Promise.all(sources.map(async (source) => {
    try {
      return [source.type, await source.load()] as const
    } catch {
      return [source.type, [] as WorkbenchResource[]] as const
    }
  }))
  const next = emptyOptions()
  for (const [type, list] of results) next[type] = list
  options.value = next
  loading.value = false
})

const selectOptions = computed(() => {
  const out = {} as Record<ContextChipType, { value: string; label: string }[]>
  for (const { type } of sources) out[type] = options.value[type].map(r => ({ value: r.id, label: r.name }))
  return out
})

const pickedCount = computed(() => Object.values(picked.value).reduce((total, values) => total + values.length, 0))
// 降级提示只看**后端资源类**（知识库/Agent/技能/工作流/插件）：记忆分类是固定枚举、
// 不依赖后端接口，若把它算进来，"还没有可用资源"这个提示将永远不会出现。
const RESOURCE_TYPES: ContextChipType[] = ['kb', 'agent', 'skill', 'workflow', 'plugin']
const hasAnyResource = computed(() => RESOURCE_TYPES.some(t => options.value[t].length > 0))

function startChat() {
  const chips: ContextChip[] = []
  for (const { type } of sources) {
    for (const value of picked.value[type]) chips.push({ type, label: value, value })
  }
  // query 约定（同名参数可重复）与对话页读 URL 共用 buildContextQuery/parseContextQuery
  router.push({ path: '/chat', query: buildContextQuery(chips) })
}
</script>

<template>
  <section class="quickstart">
    <h2 class="quickstart-title">
      {{ $t('带着工作台能力开对话') }}
    </h2>
    <p class="quickstart-sub">
      勾选这次要用到的能力，进入对话后它们会作为上下文生效（可多选）。
      多个知识库会合并检索；多个 Agent 取第一个为主、其余作为可委派的专家；多个工作流按勾选顺序依次执行。
    </p>
    <div class="quickstart-grid">
      <div
        v-for="source in sources"
        :key="source.type"
        class="quickstart-field"
      >
        <label class="quickstart-label">{{ source.label }}</label>
        <Select
          v-model:value="picked[source.type]"
          mode="multiple"
          :options="selectOptions[source.type]"
          :placeholder="source.placeholder"
          :loading="loading"
          :disabled="!loading && options[source.type].length === 0"
          allow-clear
          show-search
          option-filter-prop="label"
          class="quickstart-select"
        />
      </div>
    </div>
    <div class="quickstart-actions">
      <Button
        type="primary"
        :disabled="pickedCount === 0"
        @click="startChat"
      >
        开始对话{{ pickedCount ? `（${pickedCount}）` : '' }}
      </Button>
      <span v-if="!loading && !hasAnyResource" class="quickstart-hint">
        {{ $t('还没有可用资源；也可以先进任一工作台，在详情里点「在对话中使用」') }}
      </span>
    </div>
  </section>
</template>

<style scoped>
.quickstart {
  max-width: var(--chat-content-width);
  margin: 0 auto;
  padding: 0 var(--space-6) var(--space-12);
}

.quickstart-title {
  font-size: var(--fs-2xl);
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: var(--space-2);
}

.quickstart-sub {
  font-size: var(--fs-base);
  color: var(--text-secondary);
  margin-bottom: var(--space-5);
}

.quickstart-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}

.quickstart-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.quickstart-label {
  font-size: var(--fs-sm);
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--text-secondary);
}

.quickstart-select {
  width: 100%;
}

.quickstart-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.quickstart-hint {
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}

@media (max-width: 640px) {
  .quickstart {
    padding: 0 var(--space-4) var(--space-10);
  }
}
</style>
