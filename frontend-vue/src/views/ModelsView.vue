<script setup lang="ts">
/**
 * 模型配置：决定 `/chat` 的模型下拉里**能选到什么**。
 *
 * 背景：`/v1/models` 只返回 `llm_models` 里 `enabled` 的行，而这些行平时靠**模型发现**
 * （按 provider 的 keyset 实时拉取 /models 写入）维护。一旦发现不成功（provider 不提供
 * /models、UA 被拦、网络不通），下拉就是空的 —— 此前前端又没有任何配置入口，
 * 于是"切不了模型"。本页提供三件事：
 *   1. 看到当前有哪些模型行（含被禁用的）；
 *   2. 启用/禁用、改上下文窗口、删除；
 *   3. **手动添加**模型（发现失败时的兜底），或点「重新发现」让后端再拉一次。
 *
 * 2026-10-22 增强：分组（provider → 名称前缀）+ 折叠 + 批量开关。
 * 动因是聚合型 provider（openrouter / siliconflow / 本地 ollama）一个 provider 下挂着
 * 几百个模型，平铺列表里"把某一族全部关掉"要点几百次；现在一次勾选、一次提交。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Button, Checkbox, Input, InputNumber, Modal, Popconfirm, Select, Switch, Tag, message } from 'ant-design-vue'
import { DeleteOutlined, DownOutlined, PlusOutlined, ReloadOutlined, RightOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { listModels } from '../api'
import { TOUR_ANCHORS } from '../composables/useOnboardingTour'
import {
  batchUpdateAdminModels,
  deleteAdminModel,
  listAdminModels,
  listProviderOptions,
  updateAdminModel,
  upsertAdminModel,
  type AdminModel,
  type ProviderOption,
} from '../api/modelAdmin'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import { groupModelsByProvider } from '../utils/modelGroups'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const loading = ref(true)
const error = ref(false)
const models = ref<AdminModel[]>([])
const providers = ref<ProviderOption[]>([])
const keyword = ref('')
const busyId = ref('')

const addOpen = ref(false)
const saving = ref(false)
const draft = ref({ provider: '', name: '', display_name: '', context_window: 128000 })

// ── 折叠状态（持久化）──
// 存抽象键而不是索引：模型发现会随时增删行，按下标记折叠态会错位。
const COLLAPSE_STORAGE_KEY = 'modelsView:collapsed:v1'
const collapsed = ref<Set<string>>(readCollapsed())

function readCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSE_STORAGE_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return new Set(Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === 'string') : [])
  } catch {
    return new Set()
  }
}

watch(collapsed, (next) => {
  try {
    localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify([...next]))
  } catch {
    // 隐私模式 / 配额满：折叠态存不下不影响使用，静默降级为"本次会话内有效"
  }
})

// ── 选择状态 ──
const selectedIds = ref<Set<string>>(new Set())
/** 正在提交的方向；空串表示没有批量操作在跑（两个按钮据此各显示各的 loading） */
const batchPending = ref<'' | 'enable' | 'disable'>('')

const filtered = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  if (!q) return models.value
  return models.value.filter(m =>
    `${m.provider} ${m.name} ${m.display_name || ''}`.toLowerCase().includes(q))
})

const groups = computed(() => groupModelsByProvider(filtered.value))
const enabledCount = computed(() => models.value.filter(m => m.enabled).length)

/** 搜索时**忽略**折叠状态：命中的组却因折叠而看不见，会被当成"没搜到" */
const searching = computed(() => keyword.value.trim() !== '')
function isHidden(key: string): boolean {
  return !searching.value && collapsed.value.has(key)
}

function toggleCollapse(key: string) {
  const next = new Set(collapsed.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsed.value = next
}

function expandAll() {
  collapsed.value = new Set()
}

function collapseAll() {
  const keys: string[] = []
  for (const g of groups.value) {
    keys.push(g.key)
    // 只有一个子组时不渲染子组标题，也就无所谓它的折叠态
    if (g.subgroups.length > 1) for (const s of g.subgroups) keys.push(s.key)
  }
  collapsed.value = new Set(keys)
}

function idsOf(items: AdminModel[]): string[] {
  return items.map(m => m.id)
}

/** 三态：全选 / 部分选中 / 全不选。空集合一律当"未选"，避免空组显示成已全选 */
function selectState(ids: string[]): { checked: boolean; indeterminate: boolean } {
  if (!ids.length) return { checked: false, indeterminate: false }
  let hit = 0
  for (const id of ids) if (selectedIds.value.has(id)) hit++
  return { checked: hit === ids.length, indeterminate: hit > 0 && hit < ids.length }
}

function setSelection(ids: string[], selected: boolean) {
  const next = new Set(selectedIds.value)
  for (const id of ids) {
    if (selected) next.add(id)
    else next.delete(id)
  }
  selectedIds.value = next
}

/** 反选：这一批里已选的去掉、没选的加上（组内取补） */
function invertSelection(ids: string[]) {
  const next = new Set(selectedIds.value)
  for (const id of ids) {
    if (next.has(id)) next.delete(id)
    else next.add(id)
  }
  selectedIds.value = next
}

function clearSelection() {
  selectedIds.value = new Set()
}

/**
 * 分组视图：把复选框的三态在 computed 里算好，避免模板里对每个组重复扫描一遍
 * （几百行 × 每次重渲染 × 每组两次调用）。
 */
const groupViews = computed(() => groups.value.map(g => ({
  ...g,
  state: selectState(idsOf(g.models)),
  subgroups: g.subgroups.map(s => ({
    ...s,
    state: selectState(idsOf(s.models)),
  })),
})))

const allIds = computed(() => idsOf(filtered.value))
const allState = computed(() => selectState(allIds.value))

async function load() {
  loading.value = true
  error.value = false
  try {
    models.value = await listAdminModels()
    // 选中集里可能留着一批已经不存在（被重新发现清掉、或别处删除）的 id，
    // 它们会悄悄把"已选 N 项"顶高、还会混进批量请求里，这里顺手剔掉。
    const alive = new Set(models.value.map(m => m.id))
    if ([...selectedIds.value].some(id => !alive.has(id))) {
      selectedIds.value = new Set([...selectedIds.value].filter(id => alive.has(id)))
    }
  } catch {
    error.value = true
    models.value = []
  } finally {
    loading.value = false
  }
}

async function loadProviders() {
  try {
    providers.value = await listProviderOptions()
  } catch {
    providers.value = []
  }
}

/** 「重新发现」：先打一次 /v1/models（后端按 keyset 实时拉取并写回 llm_models），再刷新列表 */
async function rediscover() {
  loading.value = true
  try {
    await listModels()
    await load()
    message.success(t('agent.model_list_re_pulled'))
  } catch {
    message.error(t('errors.pull_failed_please_check_the_provider_s_key_and_network_you_can_also_add_a_model_manually'))
    await load()
  } finally {
    loading.value = false
  }
}

async function toggleEnabled(m: AdminModel, value: boolean) {
  busyId.value = m.id
  try {
    await updateAdminModel(m.id, { enabled: value })
    m.enabled = value
    message.success(value ? t('agent.enabled_appears_in_the_model_dropdown_on_the_conversation_page') : t('common.disabled_no_longer_appears_in_the_dropdown'))
  } catch {
    message.error(t('errors.update_failed'))
  } finally {
    busyId.value = ''
  }
}

/**
 * 批量启用/禁用：一次请求、一条 UPDATE。
 *
 * 成功后只**就地改写**本地行的 enabled，不整表重拉 —— 重拉会连折叠态之外的
 * 滚动位置、正在编辑的上下文窗口输入框一起冲掉。
 */
async function applyBatch(enabled: boolean) {
  const ids = [...selectedIds.value]
  if (!ids.length || batchPending.value) return
  batchPending.value = enabled ? 'enable' : 'disable'
  try {
    const updated = await batchUpdateAdminModels(ids, enabled)
    const touched = new Set(ids)
    for (const m of models.value) if (touched.has(m.id)) m.enabled = enabled
    clearSelection()
    message.success(t('agent.models_batch_updated', { n: updated }))
  } catch {
    message.error(t('errors.update_failed'))
  } finally {
    batchPending.value = ''
  }
}

async function saveWindow(m: AdminModel, value: number | string | null) {
  const ctx = Number(value) || 0
  if (ctx <= 0 || ctx === m.context_window) return
  busyId.value = m.id
  try {
    await updateAdminModel(m.id, { context_window: ctx })
    m.context_window = ctx
    message.success(t('chat.context_window_updated_the_context_ring_on_the_conversation_page_is_calculated_from_it'))
  } catch {
    message.error(t('errors.update_failed'))
  } finally {
    busyId.value = ''
  }
}

async function add() {
  const { provider, name, display_name, context_window } = draft.value
  if (!provider || !name.trim()) {
    message.warning(t('agent.please_select_a_provider_and_enter_the_model_name'))
    return
  }
  saving.value = true
  try {
    await upsertAdminModel({
      provider,
      name: name.trim(),
      display_name: display_name.trim() || name.trim(),
      enabled: true,
      context_window: context_window > 0 ? context_window : 128000,
    })
    addOpen.value = false
    draft.value = { provider: '', name: '', display_name: '', context_window: 128000 }
    await load()
    message.success(t('common.added_and_enabled'))
  } catch {
    message.error(t('errors.failed_to_add'))
  } finally {
    saving.value = false
  }
}

async function remove(m: AdminModel) {
  busyId.value = m.id
  try {
    await deleteAdminModel(m.id)
    models.value = models.value.filter(x => x.id !== m.id)
    if (selectedIds.value.has(m.id)) {
      const next = new Set(selectedIds.value)
      next.delete(m.id)
      selectedIds.value = next
    }
    message.success(t('common.deleted'))
  } catch {
    message.error(t('errors.delete_failed'))
  } finally {
    busyId.value = ''
  }
}

onMounted(async () => {
  await Promise.all([load(), loadProviders()])
})
</script>

<template>
  <div class="models-view">
    <header class="mv-head">
      <div>
        <h2 class="mv-title">
          {{ $t('agent.model_config') }}
        </h2>
        <p class="mv-sub">
          {{ $t('agent.only_enabled_models_appear_in_the_conversation_page_model_dropdown_total') }}
          <strong>{{ enabledCount }}</strong> {{ $t('common.enabled') }}
        </p>
      </div>
      <div class="mv-actions">
        <Input
          v-model:value="keyword"
          class="mv-search"
          :placeholder="$t('agent.search_model_name_provider')"
          allow-clear
        >
          <template #prefix>
            <SearchOutlined />
          </template>
        </Input>
        <Button
          :loading="loading"
          @click="rediscover"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('admin.rediscover_2') }}
        </Button>
        <Button
          type="primary"
          :data-tour="TOUR_ANCHORS.modelsAdd"
          @click="addOpen = true"
        >
          <template #icon>
            <PlusOutlined />
          </template>
          {{ $t('common.add_manually') }}
        </Button>
      </div>
    </header>

    <PageSkeleton v-if="loading && !models.length" />
    <EmptyState
      v-else-if="error"
      :title="$t('errors.failed_to_load_model_list')"
      :description="$t('errors.admin_permission_required_or_the_backend_was_not_restarted_the_new_v1_admin_models_route_needs_a_restart_to_take_effect')"
    />
    <EmptyState
      v-else-if="!filtered.length"
      :title="$t('agent.no_models_yet')"
      :description="$t('agent.you_can_click_rediscover_to_auto_pull_via_the_configured_key_or_use_add_manually_to_write_a_model_directly')"
    />

    <template v-else>
      <div class="mv-toolbar">
        <Checkbox
          :checked="allState.checked"
          :indeterminate="allState.indeterminate"
          @change="(e: any) => setSelection(allIds, Boolean(e.target.checked))"
        >
          {{ $t('agent.models_select_all') }}
        </Checkbox>
        <span class="mv-picked">{{ $t('agent.models_selected_count', { n: selectedIds.size }) }}</span>
        <Button
          size="small"
          type="primary"
          :disabled="!selectedIds.size"
          :loading="batchPending === 'enable'"
          @click="applyBatch(true)"
        >
          {{ $t('agent.models_batch_enable') }}
        </Button>
        <Button
          size="small"
          danger
          :disabled="!selectedIds.size"
          :loading="batchPending === 'disable'"
          @click="applyBatch(false)"
        >
          {{ $t('agent.models_batch_disable') }}
        </Button>
        <Button
          size="small"
          type="text"
          :disabled="!selectedIds.size"
          @click="clearSelection"
        >
          {{ $t('agent.models_clear_selection') }}
        </Button>
        <span class="mv-grow" />
        <Button
          size="small"
          type="text"
          @click="expandAll"
        >
          {{ $t('agent.models_expand_all') }}
        </Button>
        <Button
          size="small"
          type="text"
          @click="collapseAll"
        >
          {{ $t('agent.models_collapse_all') }}
        </Button>
      </div>

      <div class="mv-row mv-row-head">
        <span />
        <span>{{ $t('agent.model_name') }}</span>
        <span>{{ $t('common.display_name') }}</span>
        <span>{{ $t('common.context_window') }}</span>
        <span>{{ $t('common.enable') }}</span>
        <span />
      </div>

      <section
        v-for="g in groupViews"
        :key="g.key"
        class="mv-group"
      >
        <header class="mv-group-head">
          <button
            type="button"
            class="mv-toggle"
            :aria-expanded="!isHidden(g.key)"
            @click="toggleCollapse(g.key)"
          >
            <DownOutlined v-if="!isHidden(g.key)" />
            <RightOutlined v-else />
            <Tag class="mv-provider">
              {{ g.label }}
            </Tag>
            <span class="mv-sum">{{ $t('agent.models_group_summary', { total: g.models.length, enabled: g.enabledCount }) }}</span>
          </button>
          <Checkbox
            :checked="g.state.checked"
            :indeterminate="g.state.indeterminate"
            @change="(e: any) => setSelection(idsOf(g.models), Boolean(e.target.checked))"
          />
          <span class="mv-quick">
            <Button
              type="link"
              size="small"
              @click="setSelection(idsOf(g.models), true)"
            >
              {{ $t('agent.models_select_all') }}
            </Button>
            <Button
              type="link"
              size="small"
              @click="invertSelection(idsOf(g.models))"
            >
              {{ $t('agent.models_invert_selection') }}
            </Button>
            <Button
              type="link"
              size="small"
              @click="setSelection(idsOf(g.models), false)"
            >
              {{ $t('agent.models_clear_selection') }}
            </Button>
          </span>
        </header>

        <template v-if="!isHidden(g.key)">
          <div
            v-for="s in g.subgroups"
            :key="s.key"
            class="mv-subgroup"
          >
            <header
              v-if="g.subgroups.length > 1"
              class="mv-subgroup-head"
            >
              <button
                type="button"
                class="mv-toggle"
                :aria-expanded="!isHidden(s.key)"
                @click="toggleCollapse(s.key)"
              >
                <DownOutlined v-if="!isHidden(s.key)" />
                <RightOutlined v-else />
                <span class="mv-sub-label">{{ s.label || $t('agent.models_group_other') }}</span>
                <span class="mv-sum">{{ $t('agent.models_group_summary', { total: s.models.length, enabled: s.enabledCount }) }}</span>
              </button>
              <Checkbox
                :checked="s.state.checked"
                :indeterminate="s.state.indeterminate"
                @change="(e: any) => setSelection(idsOf(s.models), Boolean(e.target.checked))"
              />
              <span class="mv-quick">
                <Button
                  type="link"
                  size="small"
                  @click="setSelection(idsOf(s.models), true)"
                >
                  {{ $t('agent.models_select_all') }}
                </Button>
                <Button
                  type="link"
                  size="small"
                  @click="invertSelection(idsOf(s.models))"
                >
                  {{ $t('agent.models_invert_selection') }}
                </Button>
                <Button
                  type="link"
                  size="small"
                  @click="setSelection(idsOf(s.models), false)"
                >
                  {{ $t('agent.models_clear_selection') }}
                </Button>
              </span>
            </header>

            <template v-if="!isHidden(s.key)">
              <div
                v-for="m in s.models"
                :key="m.id"
                class="mv-row"
              >
                <Checkbox
                  :checked="selectedIds.has(m.id)"
                  @change="(e: any) => setSelection([m.id], Boolean(e.target.checked))"
                />
                <span class="mv-name">{{ m.name }}</span>
                <span class="mv-display">{{ m.display_name || '—' }}</span>
                <InputNumber
                  :value="m.context_window"
                  :min="0"
                  :step="1000"
                  size="small"
                  :disabled="busyId === m.id"
                  @blur="(e: any) => saveWindow(m, e?.target?.value)"
                  @press-enter="(e: any) => saveWindow(m, e?.target?.value)"
                />
                <Switch
                  :checked="m.enabled"
                  :loading="busyId === m.id"
                  size="small"
                  @change="(v: any) => toggleEnabled(m, Boolean(v))"
                />
                <span class="mv-op">
                  <Popconfirm
                    :title="$t('agent.delete_this_model')"
                    @confirm="remove(m)"
                  >
                    <Button
                      type="text"
                      danger
                      size="small"
                    >
                      <template #icon>
                        <DeleteOutlined />
                      </template>
                    </Button>
                  </Popconfirm>
                </span>
              </div>
            </template>
          </div>
        </template>
      </section>
    </template>

    <Modal
      v-model:open="addOpen"
      :title="$t('agent.add_model_manually')"
      :confirm-loading="saving"
      @ok="add"
    >
      <div class="mv-form">
        <label>{{ $t('common.service_provider') }}</label>
        <Select
          v-model:value="draft.provider"
          class="mv-full"
          :placeholder="$t('common.select_a_provider_with_a_configured_key')"
          :options="providers.map(p => ({ value: p.id, label: p.hasKey ? $t('admin.providers.configuredName', { name: p.name }) : p.name }))"
        />
        <label>{{ $t('agent.model_name_id_used_to_call_the_provider') }}</label>
        <Input
          v-model:value="draft.name"
          class="mv-full"
          placeholder="deepseek-chat"
        />
        <label>{{ $t('common.display_name_optional') }}</label>
        <Input
          v-model:value="draft.display_name"
          class="mv-full"
          :placeholder="$t('common.name_shown_in_the_dropdown')"
        />
        <label>{{ $t('common.context_window_tokens') }}</label>
        <InputNumber
          v-model:value="draft.context_window"
          class="mv-full"
          :min="0"
          :step="1000"
        />
        <p class="mv-hint">
          {{ $t('errors.the_model_name_must_be_an_id_actually_supported_by_the_provider_otherwise_the_conversation_will_report_the_model_as_not_found') }}
        </p>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.models-view { padding: 16px 20px; }
.mv-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; flex-wrap: wrap; margin-bottom: 12px;
}
.mv-title { margin: 0; font-size: 16px; font-weight: var(--fw-semibold); }
.mv-sub { margin: 4px 0 0; font-size: 12px; color: var(--text-tertiary); }
.mv-actions { display: flex; align-items: center; gap: 8px; }
.mv-search { width: 220px; }

/* 批量操作条：贴在表头之上，选中的是"当前筛选结果" */
.mv-toolbar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 10px; margin-bottom: 8px;
  border: 1px solid var(--border-subtle); border-radius: 8px;
  font-size: 13px;
}
.mv-picked { font-size: 12px; color: var(--text-tertiary); }
.mv-grow { flex: 1; }

/* 行 = CSS grid：分组后每组各自成块，用 grid 而不是多个 <table> 才能保持列对齐 */
.mv-row {
  display: grid;
  grid-template-columns: 32px minmax(160px, 1fr) minmax(120px, 1fr) 150px 68px 44px;
  align-items: center; gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 13px;
}
.mv-row:last-child { border-bottom: 0; }
.mv-row-head { color: var(--text-tertiary); font-size: 12px; font-weight: var(--fw-medium); border-bottom: 1px solid var(--border-subtle); }
.mv-name { font-family: var(--font-mono, monospace); overflow-wrap: anywhere; }
.mv-display { color: var(--text-secondary); overflow-wrap: anywhere; }
.mv-op { display: flex; justify-content: flex-end; }

.mv-group { border: 1px solid var(--border-subtle); border-radius: 8px; margin-bottom: 10px; overflow: hidden; }
.mv-group-head {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 10px; border-bottom: 1px solid var(--border-subtle);
}
.mv-subgroup-head {
  display: flex; align-items: center; gap: 8px;
  padding: 2px 10px 2px 22px;
  border-bottom: 1px solid var(--border-subtle);
}
/* 组头里唯一可点的元素：折叠开关。用 <button> 而不是给 div 绑 @click，
   键盘才能聚焦（见 scripts/check-a11y.mjs 的 R1）。 */
.mv-toggle {
  display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0;
  padding: 4px 0; margin: 0;
  background: none; border: 0; cursor: pointer;
  color: inherit; font: inherit; text-align: left;
}
.mv-provider { margin: 0; }
.mv-sub-label { font-family: var(--font-mono, monospace); font-size: 12px; }
.mv-sum { font-size: 12px; color: var(--text-tertiary); white-space: nowrap; }
.mv-quick { display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.mv-quick :deep(.ant-btn) { padding-inline: 4px; }

.mv-form { display: flex; flex-direction: column; gap: 6px; }
.mv-form label { font-size: 12px; color: var(--text-tertiary); margin-top: 6px; }
.mv-full { width: 100%; }
.mv-hint { margin: 8px 0 0; font-size: 12px; color: var(--text-tertiary); }
</style>
