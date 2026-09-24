<script setup lang="ts">
import { ref, computed, onMounted, markRaw } from 'vue'
import { useRouter } from 'vue-router'
import {
  Button, Input, Modal, Switch, Tag, Popconfirm, Tabs, TabPane, message,
} from 'ant-design-vue'
import {
  ThunderboltOutlined, PlusOutlined, DeleteOutlined, SearchOutlined,
  EditOutlined, ExperimentOutlined, DownOutlined, UpOutlined, ShopOutlined,
  MessageOutlined, RobotOutlined,
} from '@ant-design/icons-vue'
import { api, listAgents, listMarket, installMarket } from '../api'
import type { MarketItem } from '../api'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import SkillMarketCard from '../components/SkillMarketCard.vue'
import AttachToAgentDialog from '../components/common/AttachToAgentDialog.vue'
import { bindingUsageOf, collectBindingUsage, type BindingUsage } from '../utils/kbUsage'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
interface Plugin {
  name: string
  command: string
  args?: string[]
  env?: Record<string, string>
  description?: string
  version?: string
  status: string
}

const router = useRouter()
const loading = ref(true)
const error = ref(false)
const plugins = ref<Plugin[]>([])
const searchQuery = ref('')
const expanded = ref<Set<string>>(new Set())
const activeTab = ref('plugins')

// ── 反向引用：这些插件被哪些 Agent 装配（agents.plugins）──
// 与知识库用同一套实现（utils/kbUsage 的 collectBindingUsage）；拉失败只让这一项为空，
// 不影响插件列表本身。
const pluginUsage = ref<Map<string, BindingUsage>>(new Map())

async function loadPluginUsage() {
  try {
    const agents = (await listAgents()) as unknown as readonly Record<string, unknown>[]
    pluginUsage.value = collectBindingUsage(agents, 'plugins')
  } catch {
    pluginUsage.value = new Map()
  }
}

/** 卡片上的用量摘要；没有被引用时返回空串（不显示空徽标） */
function usageLabel(name: string): string {
  const { agents } = bindingUsageOf(pluginUsage.value, name)
  return agents.length ? `${agents.length} 个 Agent` : ''
}

/** 悬浮提示：具体是谁在用 */
function usageTitle(name: string): string {
  const { agents } = bindingUsageOf(pluginUsage.value, name)
  return agents.length ? `Agent：${agents.join('、')}` : ''
}

// 新建/编辑
const editorOpen = ref(false)
const editingName = ref('')
const form = ref({
  name: '', command: '', argsText: '', envText: '', description: '', version: '1.0.0',
})
const saving = ref(false)

// 测试
const testingName = ref('')
const testResults = ref<Record<string, { ok: boolean; message: string }>>({})

onMounted(() => {
  loadPlugins()
  loadMarket()
  loadPluginUsage()
})

// ── MCP 市场：浏览 + 一键安装；命令未命中白名单时后端返回 403 ──
const marketItems = ref<MarketItem[]>([])
const marketLoading = ref(false)
const marketError = ref(false)
const marketInstallingId = ref<string | null>(null)

async function loadMarket() {
  marketLoading.value = true
  marketError.value = false
  try {
    marketItems.value = await listMarket('mcp')
  } catch {
    marketError.value = true
    message.error(t('获取 MCP 市场失败'))
  } finally {
    marketLoading.value = false
  }
}

async function handleMarketInstall(item: MarketItem) {
  marketInstallingId.value = item.id
  try {
    await installMarket('mcp', item.id)
    message.success(`「${item.name}」已安装`)
    await Promise.all([loadMarket(), loadPlugins()])
  } catch (e: any) {
    const raw = e?.response?.data
    const detail = raw?.message || raw?.detail || raw?.error || e?.message || ''
    if (e?.response?.status === 403 || String(detail).includes('PLUGIN_COMMAND_ALLOWLIST')) {
      message.error('安装被拒绝：该 MCP 命令不在安全白名单内，请先在「插件」中手动创建，或联系管理员加入白名单')
    } else {
      message.error('安装失败: ' + detail)
    }
  } finally {
    marketInstallingId.value = null
  }
}

const filtered = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return plugins.value
  return plugins.value.filter(p =>
    (p.name || '').toLowerCase().includes(q) || (p.description || '').toLowerCase().includes(q))
})

async function loadPlugins() {
  loading.value = true
  error.value = false
  try {
    const res = await api.get('/v1/plugins')
    plugins.value = res.data?.data || []
  } catch {
    error.value = true
    message.error(t('获取插件列表失败'))
  } finally {
    loading.value = false
  }
}

function toggleExpanded(name: string) {
  const next = new Set(expanded.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  expanded.value = next
}

/**
 * 把这个插件带进对话：本次对话只放它提供的工具（内置工具仍可用）。
 * 语义是"限定工具集"而非单纯跳转 —— 见 python-engine/app/agent/runtime.py
 * 的 _restrict_tools_to_plugins。
 */
function useInChat(p: Plugin) {
  void router.push({ path: '/chat', query: { plugin: p.name } })
}

// ── 互联互通：装配到 Agent ──
// 与"在对话中使用"的区别：这里写回 Agent 的 plugins 列，是**持久**绑定 ——
// 之后每次派发该 Agent 都只放这些插件提供的工具（见 agents.plugins 迁移）。
const attachOpen = ref(false)
const attachPlugin = ref('')

function openAttach(p: Plugin) {
  attachPlugin.value = p.name
  attachOpen.value = true
}

// ── 新建/编辑 ──
function openCreate() {
  editingName.value = ''
  form.value = { name: '', command: '', argsText: '', envText: '', description: '', version: '1.0.0' }
  editorOpen.value = true
}

function openEdit(p: Plugin) {
  editingName.value = p.name
  form.value = {
    name: p.name,
    command: p.command || '',
    argsText: (p.args || []).join('\n'),
    envText: Object.entries(p.env || {}).map(([k, v]) => `${k}=${v}`).join('\n'),
    description: p.description || '',
    version: p.version || '1.0.0',
  }
  editorOpen.value = true
}

function parseLines(text: string): string[] {
  return text.split('\n').map(s => s.trim()).filter(Boolean)
}

function parseEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of parseLines(text)) {
    const i = line.indexOf('=')
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim()
  }
  return out
}

async function savePlugin() {
  if (!form.value.name.trim()) { message.warning(t('请输入名称')); return }
  if (!form.value.command.trim()) { message.warning(t('请输入 command')); return }
  const body = {
    name: form.value.name.trim(),
    command: form.value.command.trim(),
    args: parseLines(form.value.argsText),
    env: parseEnv(form.value.envText),
    description: form.value.description,
    version: form.value.version || '1.0.0',
  }
  saving.value = true
  try {
    if (editingName.value) {
      // 编辑：更新配置（PUT）；名称不可改
      await api.put(`/v1/plugins/${encodeURIComponent(editingName.value)}`, {
        command: body.command, args: body.args, env: body.env,
        description: body.description, version: body.version,
      })
      message.success(t('已保存'))
    } else {
      // 新建：install（POST）
      await api.post(`/v1/plugins/${encodeURIComponent(body.name)}/install`, body)
      message.success(t('插件已创建'))
    }
    editorOpen.value = false
    await loadPlugins()
  } catch (e: any) {
    message.error(e.response?.data?.error || e.response?.data?.detail || e.message || t('保存失败'))
  } finally {
    saving.value = false
  }
}

// ── 启停 ──
async function toggleStatus(p: Plugin, v: boolean) {
  try {
    await api.put(`/v1/plugins/${encodeURIComponent(p.name)}`, { status: v ? 'active' : 'inactive' })
    p.status = v ? 'active' : 'inactive'
    message.success(`已${v ? t('启用') : t('停用')} ${p.name}`)
  } catch (e: any) {
    message.error(e.response?.data?.error || t('操作失败'))
  }
}

// ── 卸载 ──
function requestUninstall(p: Plugin) {
  Modal.confirm({
    title: t('卸载插件'),
    content: `确定卸载「${p.name}」？其 MCP 配置将被删除。`,
    okText: t('卸载'),
    okButtonProps: { danger: true },
    cancelText: t('取消'),
    onOk: async () => {
      try {
        await api.delete(`/v1/plugins/${encodeURIComponent(p.name)}`)
        message.success(t('已卸载'))
        await loadPlugins()
      } catch {
        message.error(t('卸载失败'))
      }
    },
  })
}

// ── 连接测试 ──
async function testPlugin(p: Plugin) {
  testingName.value = p.name
  testResults.value = { ...testResults.value, [p.name]: { ok: false, message: '测试中…' } }
  try {
    const res = await api.post(`/v1/plugins/${encodeURIComponent(p.name)}/test`)
    const data = res.data?.data || {}
    testResults.value = { ...testResults.value, [p.name]: { ok: !!data.ok, message: data.message || '' } }
    if (data.ok) message.success(`${p.name} 连接正常`)
    else message.error(`${p.name} 连接失败`)
  } catch (e: any) {
    testResults.value = { ...testResults.value, [p.name]: { ok: false, message: e.response?.data?.error || e.message || '测试失败' } }
  } finally {
    testingName.value = ''
  }
}
</script>

<template>
  <div class="plugins-page">
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="page-title">
          {{ $t('插件') }}
        </h1>
        <p class="page-sub">
          {{ $t('管理 MCP 服务器配置，扩展 Agent 工具能力') }}
        </p>
      </div>
      <Button
        type="primary"
        @click="openCreate"
      >
        <template #icon>
          <PlusOutlined />
        </template>
        {{ $t('新建插件') }}
      </Button>
    </div>

    <Tabs
      v-model:active-key="activeTab"
      class="plugins-tabs"
    >
      <!-- ── 插件列表 ── -->
      <TabPane
        key="plugins"
        tab="插件"
      >
        <div class="list-toolbar">
          <Input
            v-model:value="searchQuery"
            :placeholder="$t('搜索插件（名称 / 描述）')"
            allow-clear
            class="search-input"
          >
            <template #prefix>
              <SearchOutlined />
            </template>
          </Input>
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
          :icon="markRaw(ThunderboltOutlined)"
          :description="$t('加载失败')"
          :hint="$t('无法获取插件列表，请稍后重试')"
        >
          <Button
            type="primary"
            @click="loadPlugins"
          >
            {{ $t('重试') }}
          </Button>
        </EmptyState>
        <EmptyState
          v-else-if="filtered.length === 0"
          size="page"
          :icon="markRaw(ThunderboltOutlined)"
          :description="searchQuery ? '暂无匹配的插件' : '暂无插件'"
          :hint="searchQuery ? '尝试调整搜索关键词' : '点击右上角「新建插件」，配置命令行工具集成'"
        >
          <Button
            v-if="!searchQuery"
            type="primary"
            @click="openCreate"
          >
            <template #icon>
              <PlusOutlined />
            </template>
            {{ $t('新建插件') }}
          </Button>
        </EmptyState>

        <div
          v-else
          class="plugin-grid"
        >
          <div
            v-for="p in filtered"
            :key="p.name"
            class="plugin-card"
            :class="{ inactive: p.status !== 'active' }"
          >
            <div class="card-top">
              <span class="card-icon"><ThunderboltOutlined /></span>
              <div class="card-titles">
                <span class="plugin-name">{{ p.name }}</span>
                <span class="plugin-desc">{{ p.description || '暂无描述' }}</span>
              </div>
              <Switch
                :checked="p.status === 'active'"
                size="small"
                :checked-children="'开'"
                :un-checked-children="'关'"
                @change="(v: any) => toggleStatus(p, Boolean(v))"
              />
            </div>
            <div class="card-meta">
              <Tag :color="p.status === 'active' ? 'green' : 'default'">
                {{ p.status === 'active' ? '启用' : '已停用' }}
              </Tag>
              <Tag>v{{ p.version || '1.0.0' }}</Tag>
              <span class="card-command">{{ p.command }}</span>
              <Tag
                v-if="usageLabel(p.name)"
                color="blue"
                :title="usageTitle(p.name)"
              >
                被 {{ usageLabel(p.name) }} 使用
              </Tag>
            </div>
            <div class="card-actions">
              <Button
                size="small"
                :loading="testingName === p.name"
                @click="testPlugin(p)"
              >
                <template #icon>
                  <ExperimentOutlined />
                </template>
                {{ $t('测试连接') }}
              </Button>
              <Button
                size="small"
                :title="$t('把该插件带进对话（本次对话只放它提供的工具）')"
                @click="useInChat(p)"
              >
                <template #icon>
                  <MessageOutlined />
                </template>
                {{ $t('在对话中使用') }}
              </Button>
              <Button
                size="small"
                :title="$t('把该插件装配到某个 Agent（持久绑定）')"
                @click="openAttach(p)"
              >
                <template #icon>
                  <RobotOutlined />
                </template>
                {{ $t('装配到 Agent') }}
              </Button>
              <Button
                size="small"
                type="text"
                @click="toggleExpanded(p.name)"
              >
                {{ expanded.has(p.name) ? '收起配置' : '查看配置' }}
                <UpOutlined
                  v-if="expanded.has(p.name)"
                  class="mini-icon"
                />
                <DownOutlined
                  v-else
                  class="mini-icon"
                />
              </Button>
              <div class="action-right">
                <Button
                  type="text"
                  size="small"
                  :title="$t('编辑')"
                  @click="openEdit(p)"
                >
                  <template #icon>
                    <EditOutlined />
                  </template>
                </Button>
                <Popconfirm
                  :title="$t('确认卸载？')"
                  @confirm="requestUninstall(p)"
                >
                  <Button
                    type="text"
                    danger
                    size="small"
                    :title="$t('卸载')"
                  >
                    <template #icon>
                      <DeleteOutlined />
                    </template>
                  </Button>
                </Popconfirm>
              </div>
            </div>

            <!-- 测试结果 -->
            <div
              v-if="testResults[p.name]"
              class="test-result"
              :class="{ ok: testResults[p.name].ok }"
            >
              {{ testResults[p.name].ok ? '✅' : '❌' }} {{ testResults[p.name].message }}
            </div>

            <!-- 配置详情 -->
            <div
              v-if="expanded.has(p.name)"
              class="plugin-detail"
            >
              <div class="detail-row">
                <span class="detail-label">Command</span>
                <code class="detail-code">{{ p.command }}</code>
              </div>
              <div
                v-if="p.args?.length"
                class="detail-row"
              >
                <span class="detail-label">Args</span>
                <div class="detail-code-block">
                  <div
                    v-for="a in p.args"
                    :key="a"
                    class="detail-line"
                  >
                    {{ a }}
                  </div>
                </div>
              </div>
              <div
                v-if="p.env && Object.keys(p.env).length"
                class="detail-row"
              >
                <span class="detail-label">Env</span>
                <div class="detail-code-block">
                  <div
                    v-for="(v, k) in p.env"
                    :key="k"
                    class="detail-line"
                  >
                    {{ k }}={{ v }}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </TabPane>

      <!-- ── MCP 市场 ── -->
      <TabPane
        key="market"
        tab="MCP 市场"
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
          type="mcp"
          :installing-id="marketInstallingId"
          @install="handleMarketInstall"
        />
      </TabPane>
    </Tabs>

    <!-- 新建/编辑 Modal -->
    <Modal
      :open="editorOpen"
      :title="editingName ? `编辑「${editingName}」` : '新建 MCP 插件'"
      :confirm-loading="saving"
      width="560px"
      :ok-text="$t('保存')"
      :cancel-text="$t('取消')"
      @ok="savePlugin"
      @cancel="editorOpen = false"
    >
      <div class="editor-form">
        <div class="form-row">
          <label class="form-label">{{ $t('名称 *') }}</label>
          <Input
            v-model:value="form.name"
            :placeholder="$t('如：github-mcp')"
            :disabled="!!editingName"
            :maxlength="60"
          />
        </div>
        <div class="form-row">
          <label class="form-label">Command *</label>
          <Input
            v-model:value="form.command"
            :placeholder="$t('MCP server 启动命令，如：npx、python、/path/to/server')"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('Args（每行一个）') }}</label>
          <Input.TextArea
            v-model:value="form.argsText"
            :rows="3"
            placeholder="-y&#10;@modelcontextprotocol/server-github"
            class="mono-input"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('Env（每行 KEY=VALUE）') }}</label>
          <Input.TextArea
            v-model:value="form.envText"
            :rows="3"
            placeholder="GITHUB_TOKEN=ghp_xxx"
            class="mono-input"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('描述') }}</label>
          <Input
            v-model:value="form.description"
            :maxlength="200"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('版本') }}</label>
          <Input
            v-model:value="form.version"
            placeholder="1.0.0"
          />
        </div>
      </div>
    </Modal>

    <!-- 装配到 Agent：写回 Agent 的 plugins 列（持久绑定，区别于"在对话中使用"） -->
    <AttachToAgentDialog
      v-model:open="attachOpen"
      kind="plugin"
      :value="attachPlugin"
      :label="attachPlugin"
    />
  </div>
</template>

<style scoped>
.plugins-page { max-width: 1080px; margin: 0 auto; padding: 28px 24px 60px; }
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.page-title { font-size: 24px; font-weight: 700; margin: 0; letter-spacing: -0.01em; }
.page-sub { margin: 4px 0 0; color: var(--text-tertiary); font-size: 13px; }
.plugins-tabs :deep(.ant-tabs-nav) { margin-bottom: 16px; }
.list-toolbar { margin-bottom: 16px; }
.search-input { max-width: 320px; }
.page-empty { padding: 60px 0; }
.plugin-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
.plugin-card {
  display: flex; flex-direction: column; gap: 10px;
  padding: 16px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  transition: transform var(--dur-normal) ease, border-color var(--dur-normal) ease, box-shadow var(--dur-normal) ease;
}
.plugin-card:hover { transform: translateY(-2px); border-color: var(--primary); box-shadow: var(--shadow-lg); }
.plugin-card.inactive { opacity: 0.65; }
.card-top { display: flex; align-items: flex-start; gap: 10px; }
.card-icon { flex: none; width: 36px; height: 36px; border-radius: 10px; background: var(--primary-bg); color: var(--primary); display: inline-flex; align-items: center; justify-content: center; font-size: 17px; }
.card-titles { flex: 1; min-width: 0; }
.plugin-name { display: block; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.plugin-desc { display: block; margin-top: 3px; font-size: 12px; color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; }
.card-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.card-command { font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.mini-icon { font-size: 10px; }
.action-right { margin-left: auto; display: flex; gap: 2px; }
.test-result { font-size: 12px; padding: 6px 10px; border-radius: 8px; background: var(--error-bg); color: var(--error); }
.test-result.ok { background: var(--success-bg); color: var(--success); }
.plugin-detail { display: flex; flex-direction: column; gap: 8px; border-top: 1px solid var(--border-card); padding-top: 10px; }
.detail-row { display: flex; flex-direction: column; gap: 4px; }
.detail-label { font-size: 11px; color: var(--text-tertiary); font-weight: 500; }
.detail-code { font-family: var(--font-mono); font-size: 12px; color: var(--text-primary); word-break: break-all; }
.detail-code-block { background: var(--bg-secondary); border-radius: 8px; padding: 8px; }
.detail-line { font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary); word-break: break-all; }
.editor-form { display: flex; flex-direction: column; gap: 12px; }
.form-row { display: flex; flex-direction: column; gap: 6px; }
.form-label { font-size: 12px; color: var(--text-secondary); font-weight: 500; }
.mono-input :deep(textarea) { font-family: var(--font-mono); font-size: 12px; }
@media (max-width: 768px) {
  .plugins-page { padding: 20px 16px 48px; }
  .search-input { max-width: none; width: 100%; }
  .card-actions { flex-wrap: wrap; }
}

@media (max-width: 640px) {
  .plugin-grid { grid-template-columns: 1fr; }
  .page-head { flex-direction: column; align-items: flex-start; }
  .page-head > .ant-btn { width: 100%; }
}
</style>
