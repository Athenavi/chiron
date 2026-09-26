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
 */
import { computed, onMounted, ref } from 'vue'
import { Button, Input, InputNumber, Modal, Popconfirm, Select, Switch, Tag, message } from 'ant-design-vue'
import { DeleteOutlined, PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { listModels } from '../api'
import {
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

const filtered = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  if (!q) return models.value
  return models.value.filter(m =>
    `${m.provider} ${m.name} ${m.display_name || ''}`.toLowerCase().includes(q))
})

const enabledCount = computed(() => models.value.filter(m => m.enabled).length)

async function load() {
  loading.value = true
  error.value = false
  try {
    models.value = await listAdminModels()
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
    <table
      v-else
      class="mv-table"
    >
      <thead>
        <tr>
          <th>{{ $t('common.provider') }}</th>
          <th>{{ $t('agent.model_name') }}</th>
          <th>{{ $t('common.display_name') }}</th>
          <th class="col-ctx">
            {{ $t('common.context_window') }}
          </th>
          <th class="col-on">
            {{ $t('common.enable') }}
          </th>
          <th class="col-op" />
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="m in filtered"
          :key="m.id"
        >
          <td><Tag>{{ m.provider }}</Tag></td>
          <td class="mv-name">
            {{ m.name }}
          </td>
          <td class="mv-display">
            {{ m.display_name || '—' }}
          </td>
          <td class="col-ctx">
            <InputNumber
              :value="m.context_window"
              :min="0"
              :step="1000"
              size="small"
              :disabled="busyId === m.id"
              @blur="(e: any) => saveWindow(m, e?.target?.value)"
              @press-enter="(e: any) => saveWindow(m, e?.target?.value)"
            />
          </td>
          <td class="col-on">
            <Switch
              :checked="m.enabled"
              :loading="busyId === m.id"
              size="small"
              @change="(v: any) => toggleEnabled(m, Boolean(v))"
            />
          </td>
          <td class="col-op">
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
          </td>
        </tr>
      </tbody>
    </table>

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
.mv-title { margin: 0; font-size: 16px; font-weight: 600; }
.mv-sub { margin: 4px 0 0; font-size: 12px; color: var(--text-tertiary); }
.mv-actions { display: flex; align-items: center; gap: 8px; }
.mv-search { width: 220px; }
.mv-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.mv-table th, .mv-table td {
  padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border-subtle);
}
.mv-table th { font-weight: 500; color: var(--text-tertiary); font-size: 12px; }
.mv-name { font-family: var(--font-mono, monospace); }
.mv-display { color: var(--text-secondary); }
.col-ctx { width: 160px; }
.col-on { width: 70px; }
.col-op { width: 48px; }
.mv-form { display: flex; flex-direction: column; gap: 6px; }
.mv-form label { font-size: 12px; color: var(--text-tertiary); margin-top: 6px; }
.mv-full { width: 100%; }
.mv-hint { margin: 8px 0 0; font-size: 12px; color: var(--text-tertiary); }
</style>
