<script setup lang="ts">
import { ref, computed, onMounted, markRaw } from 'vue'
import { useRouter } from 'vue-router'
import {
  Button, Input, Tabs, TabPane, Switch, Tag, Modal, InputNumber,
  Select, Alert, Dropdown, Menu, MenuItem, message,
} from 'ant-design-vue'
import {
  ThunderboltOutlined, DownloadOutlined, DeleteOutlined, PlayCircleOutlined,
  SearchOutlined, CodeOutlined, MessageOutlined, ShopOutlined, RobotOutlined,
} from '@ant-design/icons-vue'
import { api, listAgents, listMarket, installMarket } from '../api'
import type { MarketItem } from '../api'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import SkillMarketCard from '../components/SkillMarketCard.vue'
import AttachToAgentDialog from '../components/common/AttachToAgentDialog.vue'
import { collectSkillUsage, usageOf, type SkillUsage } from '../utils/skillUsage'

import { useI18n } from 'vue-i18n'
const { t: tr } = useI18n()
interface SkillParam {
  name: string
  type?: string
  description?: string
  required?: boolean
  default?: any
  enum?: string[]
}

interface Skill {
  name: string
  description: string
  version: string
  author?: string
  tags?: string[]
  exec: { type: string; source?: string }
  parameters: SkillParam[]
  installed_at?: number
  enabled?: boolean
}

const skills = ref<Skill[]>([])
const loading = ref(true)
const error = ref(false)
const activeTab = ref('list')
const searchQuery = ref('')
const typeFilter = ref('all')

const execColors: Record<string, string> = {
  python: 'purple',
  shell: 'green',
  http: 'gold',
  prompt: 'blue',
}
const router = useRouter()

// ── 加载 ──
async function loadSkills() {
  loading.value = true
  error.value = false
  try {
    const response = await api.get('/v1/skills')
    skills.value = response.data?.data?.skills || []
  } catch {
    error.value = true
    message.error(tr('获取技能列表失败，请检查网络连接'))
  } finally {
    loading.value = false
  }
}

// ── 反向引用：这些技能被哪些 Agent / 工作流在用 ──
// 两个来源互不依赖（Agent 的 skills 列 / 工作流图里的 skill 节点），
// 任一拉取失败只让那一半为空，不拖垮技能列表本身。
const skillUsage = ref<Map<string, SkillUsage>>(new Map())

async function loadSkillUsage() {
  const [agents, graphs] = await Promise.all([
    listAgents().catch(() => []),
    api.get('/v1/graphs')
      .then(r => (r.data?.data ?? []) as { name: string; graph_json?: unknown }[])
      .catch(() => [] as { name: string; graph_json?: unknown }[]),
  ])
  skillUsage.value = collectSkillUsage({ agents, graphs })
}

/** 卡片上的用量摘要；没有任何引用时返回空串（不显示空标记） */
function usageLabel(name: string): string {
  const { agents, workflows } = usageOf(skillUsage.value, name)
  const parts: string[] = []
  if (agents.length) parts.push(tr('{n} 个 Agent', { n: agents.length }))
  if (workflows.length) parts.push(tr('{n} 个工作流', { n: workflows.length }))
  return parts.join(' · ')
}

/** 悬浮提示：具体是谁在用 */
function usageTitle(name: string): string {
  const { agents, workflows } = usageOf(skillUsage.value, name)
  const lines: string[] = []
  if (agents.length) lines.push(`Agent：${agents.join('、')}`)
  if (workflows.length) lines.push(tr('工作流：{list}', { list: workflows.join('、') }))
  return lines.join('\n')
}

onMounted(() => {
  loadSkills()
  loadMarket()
  loadSkillUsage()
})

// ── 市场（技能市场：admin 发布，前端仅浏览 + 安装）──
const marketItems = ref<MarketItem[]>([])
const marketLoading = ref(false)
const marketError = ref(false)
const marketInstallingId = ref<string | null>(null)

async function loadMarket() {
  marketLoading.value = true
  marketError.value = false
  try {
    marketItems.value = await listMarket('skill')
  } catch {
    marketError.value = true
    message.error(tr('获取技能市场失败'))
  } finally {
    marketLoading.value = false
  }
}

async function handleMarketInstall(item: MarketItem) {
  marketInstallingId.value = item.id
  try {
    await installMarket('skill', item.id)
    message.success(tr('「{name}」已安装', { name: item.name }))
    await Promise.all([loadMarket(), loadSkills()])
  } catch (e: any) {
    const raw = e?.response?.data
    message.error(tr('安装失败: {error}', { error: raw?.message || raw?.detail || raw?.error || e?.message || '' }))
  } finally {
    marketInstallingId.value = null
  }
}

const execTypes = computed(() => {
  const set = new Set<string>()
  for (const s of skills.value) if (s.exec?.type) set.add(s.exec.type)
  return Array.from(set)
})

const filteredSkills = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  return skills.value.filter((s) => {
    if (typeFilter.value !== 'all' && s.exec?.type !== typeFilter.value) return false
    if (!q) return true
    return (s.name || '').toLowerCase().includes(q)
      || (s.description || '').toLowerCase().includes(q)
      || (s.tags || []).some(t => t.toLowerCase().includes(q))
  })
})

// ── 启停 ──
async function toggleEnabled(s: Skill, v: boolean) {
  try {
    await api.put(`/v1/skills/${encodeURIComponent(s.name)}`, { enabled: v })
    s.enabled = v
    if (v) message.success(tr('「{name}」已启用', { name: s.name }))
    else message.success(tr('「{name}」已停用', { name: s.name }))
  } catch (e: any) {
    message.error(tr('操作失败: {error}', { error: e?.response?.data?.detail || e?.message || '' }))
  }
}

// ── 删除 ──
function requestDelete(s: Skill) {
  Modal.confirm({
    title: tr('删除技能'),
    content: tr('确定删除「{name}」？', { name: s.name }),
    okText: tr('删除'),
    okButtonProps: { danger: true },
    cancelText: tr('取消'),
    onOk: async () => {
      try {
        await api.delete(`/v1/skills/${encodeURIComponent(s.name)}`)
        message.success(tr('已删除'))
        await loadSkills()
      } catch {
        message.error(tr('删除失败'))
      }
    },
  })
}

// ── 详情展开 ──
const expandedNames = ref<Set<string>>(new Set())
function toggleDetail(name: string) {
  const next = new Set(expandedNames.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  expandedNames.value = next
}

// ── 互联互通：在对话中使用该技能 ──
function useInChat(s: Skill) {
  router.push({ path: '/chat', query: { skill: s.name } })
}

// ── 互联互通：装配到 Agent ──
// 与"在对话中使用"的区别：这里写回 Agent 的 skills 列，是**持久**绑定 ——
// 之后每次派发该 Agent 都会带上它。
const attachOpen = ref(false)
const attachSkill = ref('')

function openAttach(s: Skill) {
  attachSkill.value = s.name
  attachOpen.value = true
}

// ── 运行 ──
const runOpen = ref(false)
const runTarget = ref<Skill | null>(null)
const runValues = ref<Record<string, any>>({})
const runSubmitting = ref(false)
const runResult = ref<any>(null)

function openRun(s: Skill) {
  runTarget.value = s
  runValues.value = {}
  for (const p of s.parameters || []) {
    if (p.default !== undefined) runValues.value[p.name] = p.default
  }
  runResult.value = null
  runOpen.value = true
}

async function submitRun() {
  if (!runTarget.value) return
  const params: Record<string, any> = {}
  for (const p of runTarget.value.parameters || []) {
    const v = runValues.value[p.name]
    if (v === undefined || v === null || v === '') {
      if (p.required) { message.warning(tr('请填写参数「{name}」', { name: p.name })); return }
      continue
    }
    params[p.name] = v
  }
  runSubmitting.value = true
  runResult.value = null
  try {
    const resp = await api.post(`/v1/skills/${encodeURIComponent(runTarget.value.name)}/run`, { params })
    runResult.value = resp.data?.data || resp.data
    message.success(tr('技能执行完成'))
  } catch (e: any) {
    message.error(tr('执行失败: {error}', { error: e?.response?.data?.detail || e?.message || '' }))
  } finally {
    runSubmitting.value = false
  }
}

function renderResult(res: any): string {
  if (res?.output) return typeof res.output === 'string' ? res.output : JSON.stringify(res.output, null, 2)
  return JSON.stringify(res, null, 2)
}

// ── 安装 ──
const installURL = ref('')
const installInline = ref('')
const installLoading = ref(false)

async function handleInstall() {
  const body: any = {}
  if (installURL.value) body.url = installURL.value
  else if (installInline.value) body.inline = installInline.value
  else { message.error(tr('请输入 URL 或内联 JSON')); return }
  installLoading.value = true
  try {
    await api.post('/v1/skills/install', body)
    message.success(tr('技能已安装'))
    installURL.value = ''
    installInline.value = ''
    await loadSkills()
    activeTab.value = 'list'
  } catch (e: any) {
    message.error(tr('安装失败: {error}', { error: e?.response?.data?.detail || e?.message || '' }))
  } finally {
    installLoading.value = false
  }
}

// ── 生成 ──
const genDesc = ref('')
const genResult = ref<any>(null)
const genLoading = ref(false)

async function handleGenerate() {
  if (!genDesc.value.trim()) { message.error(tr('请输入描述')); return }
  genLoading.value = true
  genResult.value = null
  try {
    const response = await api.post('/v1/skills/generate', {
      description: genDesc.value,
      auto_install: true,
    })
    genResult.value = response.data?.data?.skill || response.data?.data
    message.success(tr('技能已生成并安装'))
    await loadSkills()
  } catch (e: any) {
    message.error(tr('生成失败: {error}', { error: e?.response?.data?.detail || e?.message || '' }))
  } finally {
    genLoading.value = false
  }
}
</script>

<template>
  <div class="skills-page">
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="page-title">
          {{ $t('技能') }}
        </h1>
        <p class="page-sub">
          {{ $t('扩展 Agent 的自定义能力，可启用、运行与 AI 生成') }}
        </p>
      </div>
      <Button
        type="primary"
        @click="activeTab = 'install'"
      >
        <template #icon>
          <DownloadOutlined />
        </template>
        {{ $t('安装技能') }}
      </Button>
    </div>

    <Tabs
      v-model:active-key="activeTab"
      class="skills-tabs"
    >
      <!-- ── 技能列表 ── -->
      <TabPane
        key="list"
        :tab="$t('技能列表')"
      >
        <div class="list-toolbar">
          <Input
            v-model:value="searchQuery"
            :placeholder="$t('搜索名称 / 描述 / 标签')"
            allow-clear
            class="search-input"
          >
            <template #prefix>
              <SearchOutlined />
            </template>
          </Input>
          <Select
            v-model:value="typeFilter"
            class="type-filter"
            :options="[{ value: 'all', label: $t('全部类型') }, ...execTypes.map(t => ({ value: t, label: t }))]"
          />
        </div>

        <PageSkeleton
          v-if="loading"
          variant="cards"
          :columns="3"
          :rows="6"
          :header="false"
        />
        <EmptyState
          v-else-if="error"
          size="page"
          :icon="markRaw(CodeOutlined)"
          :description="$t('加载失败')"
          :hint="$t('无法获取技能列表，请稍后重试')"
        >
          <Button
            type="primary"
            @click="loadSkills"
          >
            {{ $t('重试') }}
          </Button>
        </EmptyState>
        <EmptyState
          v-else-if="filteredSkills.length === 0"
          size="page"
          :icon="markRaw(CodeOutlined)"
          :description="searchQuery || typeFilter !== 'all' ? $t('暂无匹配的技能') : $t('暂无技能')"
          :hint="searchQuery || typeFilter !== 'all' ? $t('尝试调整搜索关键词或类型筛选') : $t('从市场安装技能或上传本地技能包')"
        />

        <div
          v-else
          class="skill-grid"
        >
          <div
            v-for="s in filteredSkills"
            :key="s.name"
            class="skill-card"
            :class="{ disabled: s.enabled === false }"
          >
            <div class="card-top">
              <span class="card-icon"><CodeOutlined /></span>
              <div class="card-titles">
                <div class="card-name-line">
                  <span class="card-name">{{ s.name }}</span>
                  <Tag
                    :color="execColors[s.exec?.type] || 'default'"
                    class="exec-tag"
                  >
                    {{ s.exec?.type || 'unknown' }}
                  </Tag>
                </div>
                <span class="card-desc">{{ s.description || $t('暂无描述') }}</span>
              </div>
              <Switch
                :checked="s.enabled !== false"
                size="small"
                :checked-children="$t('开')"
                :un-checked-children="$t('关')"
                @change="(v: any) => toggleEnabled(s, Boolean(v))"
              />
            </div>
            <div class="card-meta">
              <Tag>v{{ s.version }}</Tag>
              <Tag v-if="s.parameters?.length">
                {{ $t('{n} 参数', { n: s.parameters.length }) }}
              </Tag>
              <Tag
                v-for="t in (s.tags || []).slice(0, 3)"
                :key="t"
              >
                {{ t }}
              </Tag>
              <Tag
                v-if="usageLabel(s.name)"
                color="blue"
                :title="usageTitle(s.name)"
              >
                {{ $t('被 {label} 使用', { label: usageLabel(s.name) }) }}
              </Tag>
            </div>
            <div class="card-actions">
              <Button
                size="small"
                type="text"
                @click="toggleDetail(s.name)"
              >
                {{ expandedNames.has(s.name) ? $t('收起详情') : $t('查看详情') }}
              </Button>
              <div class="action-right">
                <Button
                  size="small"
                  :title="$t('在对话中使用该技能')"
                  @click="useInChat(s)"
                >
                  <template #icon>
                    <MessageOutlined />
                  </template>
                  {{ $t('在对话中使用') }}
                </Button>
                <Button
                  size="small"
                  :title="$t('把该技能装配到某个 Agent（持久绑定）')"
                  @click="openAttach(s)"
                >
                  <template #icon>
                    <RobotOutlined />
                  </template>
                  {{ $t('装配到 Agent') }}
                </Button>
                <Button
                  size="small"
                  :disabled="s.enabled === false"
                  @click="openRun(s)"
                >
                  <template #icon>
                    <PlayCircleOutlined />
                  </template>
                  {{ $t('运行') }}
                </Button>
                <Dropdown
                  trigger="click"
                  placement="bottomRight"
                >
                  <Button
                    type="text"
                    size="small"
                    :title="$t('更多操作')"
                  >
                    ⋯
                  </Button>
                  <template #overlay>
                    <Menu class="card-menu">
                      <MenuItem
                        key="delete"
                        danger
                        @click="requestDelete(s)"
                      >
                        <DeleteOutlined class="menu-icon" />{{ $t('删除') }}
                      </MenuItem>
                    </Menu>
                  </template>
                </Dropdown>
              </div>
            </div>

            <!-- 详情展开 -->
            <div
              v-if="expandedNames.has(s.name)"
              class="skill-detail"
            >
              <div class="detail-row">
                <span class="detail-label">{{ $t('执行类型') }}</span>
                <span class="detail-value">{{ s.exec?.type }}</span>
              </div>
              <div
                v-if="s.exec?.source"
                class="detail-row"
              >
                <span class="detail-label">{{ $t('执行内容') }}</span>
                <pre class="detail-pre">{{ s.exec.source }}</pre>
              </div>
              <div
                v-if="s.parameters?.length"
                class="detail-row"
              >
                <span class="detail-label">{{ $t('参数') }}</span>
                <pre class="detail-pre">{{ JSON.stringify(s.parameters, null, 2) }}</pre>
              </div>
              <div
                v-if="s.author"
                class="detail-row"
              >
                <span class="detail-label">{{ $t('作者') }}</span>
                <span class="detail-value">{{ s.author }}</span>
              </div>
            </div>
          </div>
        </div>
      </TabPane>

      <!-- ── 技能市场 ── -->
      <TabPane
        key="market"
        :tab="$t('市场')"
      >
        <PageSkeleton
          v-if="marketLoading"
          variant="cards"
          :columns="3"
          :rows="6"
          :header="false"
        />
        <EmptyState
          v-else-if="marketError"
          size="page"
          :icon="markRaw(ShopOutlined)"
          :description="$t('市场加载失败')"
          :hint="$t('无法获取市场内容，请稍后重试')"
        >
          <Button
            type="primary"
            @click="loadMarket"
          >
            {{ $t('重试') }}
          </Button>
        </EmptyState>
        <SkillMarketCard
          v-else
          :items="marketItems"
          type="skill"
          :installing-id="marketInstallingId"
          @install="handleMarketInstall"
        />
      </TabPane>

      <!-- ── 安装技能 ── -->
      <TabPane
        key="install"
        :tab="$t('安装技能')"
      >
        <div class="panel-card">
          <h3 class="panel-title">
            <DownloadOutlined /> {{ $t('从 URL 安装') }}
          </h3>
          <Input
            v-model:value="installURL"
            placeholder="https://example.com/my-skill.skill.json"
          />
          <div class="or-divider">
            {{ $t('或') }}
          </div>
          <h3 class="panel-title">
            <CodeOutlined /> {{ $t('内联 JSON') }}
          </h3>
          <Input.TextArea
            v-model:value="installInline"
            :rows="6"
            placeholder="{&quot;name&quot;:&quot;my-skill&quot;,&quot;description&quot;:&quot;...&quot;,&quot;exec&quot;:{&quot;type&quot;:&quot;prompt&quot;,&quot;source&quot;:&quot;...&quot;},&quot;parameters&quot;:[]}"
            class="mono-input"
          />
          <Button
            type="primary"
            :loading="installLoading"
            class="install-btn"
            @click="handleInstall"
          >
            <template #icon>
              <DownloadOutlined />
            </template>
            {{ $t('安装') }}
          </Button>
        </div>
      </TabPane>

      <!-- ── AI 生成 ── -->
      <TabPane
        key="generate"
        :tab="$t('AI 生成')"
      >
        <div class="panel-card">
          <h3 class="panel-title">
            <ThunderboltOutlined /> {{ $t('用一句话描述技能') }}
          </h3>
          <Input.TextArea
            v-model:value="genDesc"
            :rows="4"
            :placeholder="$t('例如：创建一个能分析 Jenkins 构建日志并汇总失败原因的技能')"
          />
          <Button
            type="primary"
            :loading="genLoading"
            class="install-btn"
            @click="handleGenerate"
          >
            <template #icon>
              <ThunderboltOutlined />
            </template>
            {{ $t('生成并安装') }}
          </Button>
          <Alert
            v-if="genResult"
            type="success"
            show-icon
            class="gen-alert"
            :message="$t('已生成：{name}', { name: genResult.name })"
            :description="genResult.description"
          />
          <pre
            v-if="genResult"
            class="gen-pre"
          >{{ JSON.stringify(genResult, null, 2) }}</pre>
        </div>
      </TabPane>
    </Tabs>

    <!-- ── 运行 Modal ── -->
    <Modal
      :open="runOpen"
      :title="$t('运行「{name}」', { name: runTarget?.name || '' })"
      :footer="null"
      width="560px"
      @cancel="runOpen = false"
    >
      <div class="run-desc">
        {{ runTarget?.description }}
      </div>
      <div
        v-if="runTarget?.parameters?.length"
        class="run-form"
      >
        <div
          v-for="p in runTarget.parameters"
          :key="p.name"
          class="run-field"
        >
          <label class="field-label">
            {{ p.name }}
            <span
              v-if="p.required"
              class="required"
            >*</span>
            <span
              v-if="p.type"
              class="field-type"
            >{{ p.type }}</span>
          </label>
          <Select
            v-if="p.enum?.length"
            v-model:value="runValues[p.name]"
            :options="p.enum.map(e => ({ value: e, label: e }))"
            :placeholder="p.description || p.name"
            style="width: 100%"
          />
          <InputNumber
            v-else-if="p.type === 'number'"
            v-model:value="runValues[p.name]"
            :placeholder="p.description || p.name"
            style="width: 100%"
          />
          <Switch
            v-else-if="p.type === 'boolean'"
            v-model:checked="runValues[p.name]"
          />
          <Input.TextArea
            v-else-if="p.type === 'text'"
            v-model:value="runValues[p.name]"
            :rows="3"
            :placeholder="p.description || p.name"
          />
          <Input
            v-else
            v-model:value="runValues[p.name]"
            :placeholder="p.description || p.name"
          />
        </div>
      </div>
      <div
        v-else
        class="run-empty"
      >
        {{ $t('该技能无需参数') }}
      </div>
      <div class="run-actions">
        <Button
          type="primary"
          :loading="runSubmitting"
          @click="submitRun"
        >
          <template #icon>
            <PlayCircleOutlined />
          </template>
          {{ $t('执行') }}
        </Button>
      </div>
      <div
        v-if="runResult"
        class="run-result"
      >
        <div class="result-label">
          {{ $t('执行结果') }}
        </div>
        <pre class="result-pre">{{ renderResult(runResult) }}</pre>
      </div>
    </Modal>

    <!-- 装配到 Agent：写回 Agent 的 skills 列（持久绑定，区别于"在对话中使用"） -->
    <AttachToAgentDialog
      v-model:open="attachOpen"
      kind="skill"
      :value="attachSkill"
      :label="attachSkill"
    />
  </div>
</template>

<style scoped>
.skills-page { max-width: 1080px; margin: 0 auto; padding: 28px 24px 60px; }
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.page-title { font-size: 24px; font-weight: 700; margin: 0; letter-spacing: -0.01em; }
.page-sub { margin: 4px 0 0; color: var(--text-tertiary); font-size: 13px; }
.skills-tabs :deep(.ant-tabs-nav) { margin-bottom: 20px; }

.list-toolbar { display: flex; gap: 10px; margin-bottom: 16px; }
.search-input { max-width: 320px; }
.type-filter { width: 140px; }

.skill-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
.skill-card {
  display: flex; flex-direction: column; gap: 12px;
  padding: 16px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  transition: transform var(--dur-normal) ease, border-color var(--dur-normal) ease, box-shadow var(--dur-normal) ease;
}
.skill-card:hover { transform: translateY(-2px); border-color: var(--primary); box-shadow: var(--shadow-lg); }
.skill-card.disabled { opacity: 0.65; }
.card-top { display: flex; align-items: flex-start; gap: 10px; }
.card-icon { flex: none; width: 36px; height: 36px; border-radius: 10px; background: var(--primary-bg); color: var(--primary); display: inline-flex; align-items: center; justify-content: center; font-size: 17px; }
.card-titles { flex: 1; min-width: 0; }
.card-name-line { display: flex; align-items: center; gap: 6px; min-width: 0; }
.card-name { font-size: 14px; font-weight: 600; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.exec-tag { flex: none; }
.card-desc { display: block; margin-top: 3px; font-size: 12px; color: var(--text-tertiary); line-height: 1.5; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.card-meta { display: flex; flex-wrap: wrap; gap: 4px; }
.card-actions { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 4px; }
.action-right { display: flex; align-items: center; gap: 4px; }
.card-menu { min-width: 130px; border-radius: 10px; padding: 4px; box-shadow: var(--shadow-lg); }
.card-menu :deep(.ant-dropdown-menu-item) { display: flex; align-items: center; gap: 8px; font-size: 13px; border-radius: 6px; }
.menu-icon { font-size: 14px; }

.skill-detail { display: flex; flex-direction: column; gap: 8px; border-top: 1px solid var(--border); padding-top: 12px; }
.detail-row { display: flex; flex-direction: column; gap: 4px; }
.detail-label { font-size: 11px; color: var(--text-tertiary); font-weight: 500; }
.detail-value { font-size: 12px; color: var(--text-primary); }
.detail-pre { margin: 0; padding: 8px; background: var(--bg-secondary); border-radius: 8px; font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary); white-space: pre-wrap; word-break: break-all; max-height: 180px; overflow-y: auto; }

.panel-card { max-width: 640px; border: 1px solid var(--border-card); border-radius: var(--radius-lg); background: var(--bg-card); box-shadow: var(--shadow-md); padding: 20px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--text-primary); margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }
.or-divider { text-align: center; color: var(--text-tertiary); margin: 14px 0; }
.mono-input :deep(textarea) { font-family: var(--font-mono); font-size: 12px; }
.install-btn { margin-top: 14px; }
.gen-alert { margin-top: 14px; }
.gen-pre { margin: 12px 0 0; padding: 10px; background: var(--bg-secondary); border-radius: 8px; font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary); white-space: pre-wrap; max-height: 260px; overflow-y: auto; }

.run-desc { font-size: 13px; color: var(--text-secondary); margin-bottom: 14px; }
.run-form { display: flex; flex-direction: column; gap: 12px; }
.run-field { display: flex; flex-direction: column; gap: 6px; }
.field-label { font-size: 12px; color: var(--text-secondary); font-weight: 500; display: flex; align-items: center; gap: 6px; }
.required { color: var(--error); }
.field-type { font-size: 10px; color: var(--text-tertiary); background: var(--bg-secondary); padding: 1px 6px; border-radius: 4px; }
.run-empty { color: var(--text-tertiary); font-size: 13px; padding: 8px 0; }
.run-actions { display: flex; justify-content: flex-end; margin-top: 14px; }
.run-result { margin-top: 14px; border: 1px solid var(--border-card); border-radius: 10px; overflow: hidden; }
.result-label { padding: 8px 12px; font-size: 12px; color: var(--text-tertiary); background: var(--bg-secondary); border-bottom: 1px solid var(--border-card); }
.result-pre { margin: 0; padding: 12px; font-family: var(--font-mono); font-size: 12px; line-height: 1.6; color: var(--text-primary); white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow-y: auto; }

@media (max-width: 768px) {
  .skills-page { padding: 20px 16px 48px; }
  .list-toolbar { flex-direction: column; align-items: stretch; }
  .search-input { max-width: none; }
  .type-filter { width: 100%; }
}

@media (max-width: 640px) {
  .skill-grid { grid-template-columns: 1fr; }
  .page-head { flex-direction: column; align-items: flex-start; }
  .page-head > .ant-btn { width: 100%; }
}
</style>
