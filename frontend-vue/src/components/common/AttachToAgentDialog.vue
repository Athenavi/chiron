<script setup lang="ts">
/**
 * 把某个工作台资源装配到 Agent：选目标 Agent → 直接写回它的 kb_id / skills / plugins。
 *
 * 与「在对话中使用」的区别：这里是**持久绑定** —— 装完后每次派发该 Agent 都会带上它，
 * 而不是只在这一次对话生效。
 *
 * 只改目标字段：`PUT /v1/agents/{id}` 在 Go 侧是动态 SET（internal/api/agents.go），
 * 因此不会覆盖 Agent 的提示词/工具/模型等其它配置；非属主会被后端的 AND user_id 拒绝。
 */
import { computed, ref, watch } from 'vue'
import { Modal, Select, message } from 'ant-design-vue'
import { listAgents, updateAgent } from '../../api'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
type AttachKind = 'kb' | 'skill' | 'plugin' | 'workflow'

const props = defineProps<{
  open: boolean
  /** 被装配的资源类型：知识库（单值覆盖）/ 技能、插件、工作流（数组追加） */
  kind: AttachKind
  /** 资源标识：kb / workflow 用 id；skill / plugin 用名字（与 Agent 对应列的存储形态一致） */
  value: string
  /** 展示名 */
  label: string
}>()

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'attached', agentId: string): void
}>()

/** 类型 → 展示名（标题与提示共用，避免两处各写一份三元表达式） */
const SUBJECT_LABEL = computed<Record<AttachKind, string>>(() => ({
  kb: t('知识库'),
  skill: t('技能'),
  plugin: t('插件'),
  workflow: t('工作流'),
}))

interface AgentOption {
  id: string
  name: string
  kb_id?: string
  skills?: string[]
  plugins?: string[]
  workflows?: string[]
}

const agents = ref<AgentOption[]>([])
const loading = ref(false)
const agentId = ref<string | undefined>(undefined)
const saving = ref(false)

const options = computed(() => agents.value.map(a => ({ value: a.id, label: a.name })))

/** 取目标 Agent 上该类绑定的当前值（各类的存放位置不同） */
function boundList(target: AgentOption): string[] {
  if (props.kind === 'kb') return target.kb_id ? [target.kb_id] : []
  if (props.kind === 'skill') return target.skills || []
  if (props.kind === 'workflow') return target.workflows || []
  return target.plugins || []
}

/** 目标 Agent 是否已经绑定了这项资源（给用户一个"别重复装"的提示） */
const alreadyBound = computed(() => {
  const target = agents.value.find(a => a.id === agentId.value)
  return target ? boundList(target).includes(props.value) : false
})

watch(() => props.open, async (open) => {
  if (!open) return
  agentId.value = undefined
  if (agents.value.length > 0) return
  loading.value = true
  try {
    agents.value = await listAgents()
  } catch {
    agents.value = []
    message.error(t('获取 Agent 列表失败'))
  } finally {
    loading.value = false
  }
})

async function attach() {
  const target = agents.value.find(a => a.id === agentId.value)
  if (!target) {
    message.warning(t('请选择目标 Agent'))
    return
  }
  saving.value = true
  try {
    if (props.kind === 'kb') {
      // 知识库是单值：直接覆盖（Agent 只有一个"默认知识库"）
      await updateAgent(target.id, { kb_id: props.value })
    } else {
      // 技能 / 插件 / 工作流是数组：追加并去重，不覆盖已绑定的其它项
      const next = Array.from(new Set([...boundList(target), props.value]))
      const field = props.kind === 'skill'
        ? 'skills'
        : props.kind === 'workflow'
          ? 'workflows'
          : 'plugins'
      await updateAgent(target.id, { [field]: next })
    }
    message.success(t('已装配到「{name}」', { name: target.name }))
    emit('attached', target.id)
    emit('update:open', false)
  } catch (e: any) {
    const raw = e?.response?.data
    // 非属主会被后端拒绝（UPDATE ... AND user_id = 当前用户）：把原因带出来，
    // 否则用户只会看到"失败"而不知道是权限问题
    const detail = raw?.message || raw?.detail || raw?.error || e?.message || ''
    message.error(t('装配失败：') + (detail || t('只能操作自己创建的 Agent')))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal
    :open="open"
    :title="$t('装配到 Agent')"
    :confirm-loading="saving"
    :ok-button-props="{ disabled: !agentId }"
    :ok-text="$t('装配')"
    :cancel-text="$t('取消')"
    @ok="attach"
    @cancel="emit('update:open', false)"
  >
    <div class="attach-form">
      <p class="attach-subject">
        {{ SUBJECT_LABEL[kind] }}：{{ label }}
      </p>
      <Select
        v-model:value="agentId"
        :options="options"
        :loading="loading"
        :placeholder="$t('选择目标 Agent')"
        show-search
        option-filter-prop="label"
        class="attach-select"
      />
      <p
        v-if="alreadyBound"
        class="attach-hint"
      >
        {{ $t('该 Agent 已装配此项，重复装配不会产生变化。') }}
      </p>
      <p
        v-else
        class="attach-hint"
      >
        {{ kind === 'kb'
          ? $t('知识库是单值：会覆盖该 Agent 原有的默认知识库。')
          : $t('{subject}会追加到该 Agent 的已有绑定，不覆盖其它项。', { subject: SUBJECT_LABEL[kind] }) }}
      </p>
    </div>
  </Modal>
</template>

<style scoped>
.attach-form { display: flex; flex-direction: column; gap: 10px; }
.attach-subject { margin: 0; font-size: 13px; font-weight: 600; color: var(--text-primary); }
.attach-select { width: 100%; }
.attach-hint { margin: 0; font-size: 12px; line-height: 1.6; color: var(--text-secondary); }
</style>
