<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { message, Modal } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { api } from '../../api'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
interface ModelRoute {
  id: string
  tenant_id: string
  model_id: string
  primary_provider: string
  fallback_order: string[]
  provider_config: Record<string, any>
  enabled: boolean
  priority: number
  created_at: string
  updated_at: string
}

const loading = ref(false)
const routes = ref<ModelRoute[]>([])

const modalVisible = ref(false)
const modalMode = ref<'create' | 'edit'>('create')
const form = ref({
  id: '',
  model_id: '',
  primary_provider: '',
  fallback_order: '',
  provider_config: '{}',
  enabled: true,
  priority: 1,
})
const saving = ref(false)

async function fetchRoutes() {
  loading.value = true
  try {
    const res = await api.get('/v1/ent/model-routes')
    routes.value = res.data?.data?.routes || []
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.failed_to_load'))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  modalMode.value = 'create'
  form.value = { id: '', model_id: '', primary_provider: '', fallback_order: '', provider_config: '{}', enabled: true, priority: 1 }
  modalVisible.value = true
}

function openEdit(r: ModelRoute) {
  modalMode.value = 'edit'
  form.value = {
    id: r.id,
    model_id: r.model_id,
    primary_provider: r.primary_provider,
    fallback_order: (r.fallback_order ?? []).join('\n'),
    provider_config: JSON.stringify(r.provider_config ?? {}, null, 2),
    enabled: r.enabled,
    priority: r.priority,
  }
  modalVisible.value = true
}

async function save() {
  const fallback = form.value.fallback_order.split('\n').map(s => s.trim()).filter(Boolean)
  let config: Record<string, any>
  try {
    config = JSON.parse(form.value.provider_config || '{}')
  } catch {
    message.error(t('common.provider_config_must_be_valid_json'))
    return
  }
  saving.value = true
  try {
    if (modalMode.value === 'create') {
      await api.post('/v1/ent/model-routes', {
        model_id: form.value.model_id,
        primary_provider: form.value.primary_provider,
        fallback_order: fallback,
        provider_config: config,
        enabled: form.value.enabled,
        priority: form.value.priority,
      })
      message.success(t('common.created'))
    } else {
      await api.put(`/v1/ent/model-routes/${form.value.id}`, {
        primary_provider: form.value.primary_provider || undefined,
        fallback_order: fallback.length > 0 ? fallback : undefined,
        provider_config: Object.keys(config).length > 0 ? config : undefined,
        enabled: form.value.enabled,
        priority: form.value.priority || undefined,
      })
      message.success(t('common.updated'))
    }
    modalVisible.value = false
    fetchRoutes()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.save_failed'))
  } finally {
    saving.value = false
  }
}

function confirmDelete(r: ModelRoute) {
  Modal.confirm({
    title: t('admin.delete_routing_rule'),
    content: t('确认删除模型「{id}」的路由规则？', { id: r.model_id }),
    okText: t('common.delete'),
    okType: 'danger',
    cancelText: t('common.cancel'),
    onOk: async () => {
      try {
        await api.delete(`/v1/ent/model-routes/${r.id}`)
        message.success(t('common.deleted'))
        fetchRoutes()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('errors.delete_failed'))
      }
    },
  })
}

async function toggleEnabled(r: ModelRoute) {
  try {
    await api.put(`/v1/ent/model-routes/${r.id}`, { enabled: !r.enabled })
    r.enabled = !r.enabled
    message.success(r.enabled ? t('common.enabled_2') : t('common.disabled_2'))
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.operation_failed'))
  }
}

const columns: TableColumnsType = [
  { title: t('agent.model_id'), dataIndex: 'model_id', key: 'model_id', width: 180 },
  { title: t('common.primary_provider'), dataIndex: 'primary_provider', key: 'primary_provider', width: 140 },
  { title: t('errors.failover_order'), key: 'fallback', width: 160, customRender: ({ record }) => (record.fallback_order ?? []).join(' → ') || '-' },
  { title: t('common.priority'), dataIndex: 'priority', key: 'priority', width: 80 },
  { title: t('common.enable'), key: 'enabled', width: 70, customRender: ({ record }) => record.enabled ? t('common.yes') : t('common.no') },
  { title: t('common.updated_at'), dataIndex: 'updated_at', key: 'updated_at', width: 170, customRender: ({ text }) => new Date(text).toLocaleString('zh-CN', { hour12: false }) },
  { title: t('common.action'), key: 'action', width: 180, fixed: 'right' },
]

onMounted(fetchRoutes)
</script>

<template>
  <div class="model-router-view">
    <div class="page-header">
      <h2 class="page-title">
        {{ $t('agent.model_routing_control') }}
      </h2>
      <a-button
        type="primary"
        @click="openCreate"
      >
        {{ $t('admin.new_route') }}
      </a-button>
    </div>

    <a-alert
      type="info"
      show-icon
      :message="$t('billing.per_tenant_model_routing_primary_provider_sets_the_preferred_provider_fallback_order_defines_the_circuit_breaker_downgrade_sequence_and_priority_sets_the_match_priority_synced_automatically_when_the_python_engine_starts')"
      style="margin-bottom: 16px"
    />

    <a-table
      :columns="columns"
      :data-source="routes"
      :loading="loading"
      :row-key="(r: ModelRoute) => r.id"
      :pagination="false"
      :scroll="{ x: 1000 }"
      size="small"
    >
      <template #emptyText>
        <div class="empty-block">
          <span class="empty-icon">📭</span>
          <span class="empty-text">{{ $t('common.no_data_yet') }}</span>
        </div>
      </template>
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'action'">
          <a-button
            type="link"
            size="small"
            @click="toggleEnabled(record as ModelRoute)"
          >
            {{ (record as ModelRoute).enabled ? $t('common.disable') : $t('common.enable') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            @click="openEdit(record as ModelRoute)"
          >
            {{ $t('common.edit_2') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            danger
            @click="confirmDelete(record as ModelRoute)"
          >
            {{ $t('common.delete') }}
          </a-button>
        </template>
      </template>
    </a-table>

    <a-modal
      v-model:open="modalVisible"
      :title="modalMode === 'create' ? $t('agent.new_model_route') : $t('agent.edit_model_route')"
      :confirm-loading="saving"
      width="640"
      @ok="save"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('agent.model_id')">
          <a-input
            v-model:value="form.model_id"
            :disabled="modalMode === 'edit'"
            :placeholder="$t('common.e_g_gpt_4_claude_3_opus')"
          />
        </a-form-item>
        <a-form-item :label="$t('common.primary_provider')">
          <a-input
            v-model:value="form.primary_provider"
            :placeholder="$t('common.e_g_openai_anthropic_deepseek')"
          />
        </a-form-item>
        <a-form-item :label="$t('errors.failover_order_one_provider_per_line')">
          <a-textarea
            v-model:value="form.fallback_order"
            :rows="4"
            class="code-editor"
            placeholder="anthropic&#10;deepseek"
          />
        </a-form-item>
        <a-form-item :label="$t('common.provider_config_json')">
          <a-textarea
            v-model:value="form.provider_config"
            :rows="4"
            class="code-editor"
            placeholder="{&quot;temperature&quot;: 0.7, &quot;max_tokens&quot;: 4096}"
          />
        </a-form-item>
        <a-form-item :label="$t('common.priority')">
          <a-input-number
            v-model:value="form.priority"
            :min="1"
            :max="999"
          />
        </a-form-item>
        <a-form-item :label="$t('common.enable')">
          <a-switch v-model:checked="form.enabled" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
.model-router-view {
  padding: 0;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.page-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}
:deep(.code-editor) {
  font-family: 'SF Mono', 'Menlo', 'Monaco', 'Consolas', monospace;
  font-size: 12px;
}
</style>