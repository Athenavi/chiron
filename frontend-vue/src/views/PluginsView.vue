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
  return agents.length ? t('{n} 个 Agent', { n: agents.length }) : ''
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
    message.error(t('errors.failed_to_fetch_mcp_marketplace'))
  } finally {
    marketLoading.value = false
  }
}

async function handleMarketInstall(item: MarketItem) {
  marketInstallingId.value = item.id
  try {
    await installMarket('mcp', item.id)
    message.success(t('「{name}」已安装', { name: item.name }))
    await Promise.all([loadMarket(), loadPlugins()])
  } catch (e: any) {
    const raw = e?.response?.data
    const detail = raw?.message || raw?.detail || raw?.error || e?.message || ''
    if (e?.response?.status === 403 || String(detail).includes('PLUGIN_COMMAND_ALLOWLIST')) {
      message.error(t('errors.installation_rejected_this_mcp_command_is_not_on_the_safe_allowlist_please_create_it_manually_under_plugins_or_ask_an_admin_to_add_it_to_the_allowlist'))
    } else {
      message.error(t('安装失败: {detail}', { detail }))
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
    message.error(t('errors.failed_to_fetch_plugin_list'))
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
  if (!form.value.name.trim()) { message.warning(t('common.please_enter_a_name')); return }
  if (!form.value.command.trim()) { message.warning(t('common.please_enter_command')); return }
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
      message.success(t('common.saved'))
    } else {
      // 新建：install（POST）
      await api.post(`/v1/plugins/${encodeURIComponent(body.name)}/install`, body)
      message.success(t('common.plugin_created'))
    }
    editorOpen.value = false
    await loadPlugins()
  } catch (e: any) {
    message.error(e.response?.data?.error || e.response?.data?.detail || e.message || t('errors.save_failed'))
  } finally {
    saving.value = false
  }
}

// ── 启停 ──
async function toggleStatus(p: Plugin, v: boolean) {
  try {
    await api.put(`/v1/plugins/${encodeURIComponent(p.name)}`, { status: v ? 'active' : 'inactive' })
    p.status = v ? 'active' : 'inactive'
    if (v) message.success(t('已启用 {name}', { name: p.name }))
    else message.success(t('已停用 {name}', { name: p.name }))
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.operation_failed'))
  }
}

// ── 卸载 ──
function requestUninstall(p: Plugin) {
  Modal.confirm({
    title: t('common.uninstall_plugin'),
    content: t('确定卸载「{name}」？其 MCP 配置将被删除。', { name: p.name }),
    okText: t('common.uninstall'),
    okButtonProps: { danger: true },
    cancelText: t('common.cancel'),
    onOk: async () => {
      try {
        await api.delete(`/v1/plugins/${encodeURIComponent(p.name)}`)
        message.success(t('common.uninstalled'))
        await loadPlugins()
      } catch {
        message.error(t('errors.uninstall_failed'))
      }
    },
  })
}

// ── 连接测试 ──
async function testPlugin(p: Plugin) {
  testingName.value = p.name
  testResults.value = { ...testResults.value, [p.name]: { ok: false, message: t('common.testing') } }
  try {
    const res = await api.post(`/v1/plugins/${encodeURIComponent(p.name)}/test`)
    const data = res.data?.data || {}
    testResults.value = { ...testResults.value, [p.name]: { ok: !!data.ok, message: data.message || '' } }
    if (data.ok) message.success(t('{name} 连接正常', { name: p.name }))
    else message.error(t('{name} 连接失败', { name: p.name }))
  } catch (e: any) {
    testResults.value = { ...testResults.value, [p.name]: { ok: false, message: e.response?.data?.error || e.message || t('errors.test_failed') } }
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
          {{ $t('common.plugin') }}
        </h1>
        <p class="page-sub">
          {{ $t('agent.manage_mcp_server_config_to_extend_agent_tool_capabilities') }}
        </p>
      </div>
      <Button
        type="primary"
        @click="openCreate"
      >
        <template #icon>
          <PlusOutlined />
        </template>
        {{ $t('common.new_plugin') }}
      </Button>
    </div>

    <Tabs
      v-model:active-key="activeTab"
      class="plugins-tabs"
    >
      <!-- ── 插件列表 ── -->
      <TabPane
        key="plugins"
        :tab="$t('common.plugin')"
      >
        <div class="list-toolbar">
          <Input
            v-model:value="searchQuery"
            :placeholder="$t('common.search_plugins_name_description')"
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
          :description="$t('errors.failed_to_load')"
          :hint="$t('common.unable_to_fetch_plugin_list_please_retry_later')"
        >
          <Button
            type="primary"
            @click="loadPlugins"
          >
            {{ $t('common.retry') }}
          </Button>
        </EmptyState>
        <EmptyState
          v-else-if="filtered.length === 0"
          size="page"
          :icon="markRaw(ThunderboltOutlined)"
          :description="searchQuery ? $t('common.no_matching_plugins') : $t('common.no_plugins')"
          :hint="searchQuery ? $t('common.try_adjusting_your_search_keywords') : $t('agent.click_new_plugin_at_the_top_right_to_configure_cli_tool_integration')"
        >
          <Button
            v-if="!searchQuery"
            type="primary"
            @click="openCreate"
          >
            <template #icon>
              <PlusOutlined />
            </template>
            {{ $t('common.new_plugin') }}
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
                <span class="plugin-desc">{{ p.description || $t('common.no_description') }}</span>
              </div>
              <Switch
                :checked="p.status === 'active'"
                size="small"
                :checked-children="$t('common.on')"
                :un-checked-children="$t('common.off')"
                @change="(v: any) => toggleStatus(p, Boolean(v))"
              />
            </div>
            <div class="card-meta">
              <Tag :color="p.status === 'active' ? 'green' : 'default'">
                {{ p.status === 'active' ? $t('common.enable') : $t('common.disabled') }}
              </Tag>
              <Tag>v{{ p.version || '1.0.0' }}</Tag>
              <span class="card-command">{{ p.command }}</span>
              <Tag
                v-if="usageLabel(p.name)"
                color="blue"
                :title="usageTitle(p.name)"
              >
                {{ $t('被 {label} 使用', { label: usageLabel(p.name) }) }}
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
                {{ $t('common.test_connection') }}
              </Button>
              <Button
                size="small"
                :title="$t('agent.bring_this_plugin_into_the_conversation_only_its_tools_are_available_in_this_conversation')"
                @click="useInChat(p)"
              >
                <template #icon>
                  <MessageOutlined />
                </template>
                {{ $t('chat.use_in_the_conversation') }}
              </Button>
              <Button
                size="small"
                :title="$t('agent.assemble_this_plugin_into_an_agent_persistent_binding')"
                @click="openAttach(p)"
              >
                <template #icon>
                  <RobotOutlined />
                </template>
                {{ $t('agent.assemble_to_agent') }}
              </Button>
              <Button
                size="small"
                type="text"
                @click="toggleExpanded(p.name)"
              >
                {{ expanded.has(p.name) ? $t('common.collapse_config') : $t('common.view_config') }}
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
                  :title="$t('common.edit_2')"
                  @click="openEdit(p)"
                >
                  <template #icon>
                    <EditOutlined />
                  </template>
                </Button>
                <Popconfirm
                  :title="$t('common.confirm_uninstall')"
                  @confirm="requestUninstall(p)"
                >
                  <Button
                    type="text"
                    danger
                    size="small"
                    :title="$t('common.uninstall')"
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
        :tab="$t('agent.mcp_market')"
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
          :description="$t('errors.failed_to_load_marketplace')"
          :hint="$t('common.unable_to_fetch_marketplace_content_please_retry_later')"
        >
          <Button
            type="primary"
            @click="loadMarket"
          >
            {{ $t('common.retry') }}
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
      :title="editingName ? $t('编辑「{name}」', { name: editingName }) : $t('agent.new_mcp_plugin')"
      :confirm-loading="saving"
      width="560px"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      @ok="savePlugin"
      @cancel="editorOpen = false"
    >
      <div class="editor-form">
        <div class="form-row">
          <label class="form-label">{{ $t('common.name_2') }}</label>
          <Input
            v-model:value="form.name"
            :placeholder="$t('agent.e_g_github_mcp')"
            :disabled="!!editingName"
            :maxlength="60"
          />
        </div>
        <div class="form-row">
          <label class="form-label">Command *</label>
          <Input
            v-model:value="form.command"
            :placeholder="$t('agent.mcp_server_start_command_e_g_npx_python_path_to_server')"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('common.args_one_per_line') }}</label>
          <Input.TextArea
            v-model:value="form.argsText"
            :rows="3"
            placeholder="-y&#10;@modelcontextprotocol/server-github"
            class="mono-input"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('common.env_one_key_value_per_line') }}</label>
          <Input.TextArea
            v-model:value="form.envText"
            :rows="3"
            placeholder="GITHUB_TOKEN=ghp_xxx"
            class="mono-input"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('common.description') }}</label>
          <Input
            v-model:value="form.description"
            :maxlength="200"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('common.version') }}</label>
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
