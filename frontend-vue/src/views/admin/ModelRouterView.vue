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
    message.error(e?.response?.data?.error || t('加载失败'))
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
    message.error(t('provider_config 必须是合法 JSON'))
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
      message.success(t('已创建'))
    } else {
      await api.put(`/v1/ent/model-routes/${form.value.id}`, {
        primary_provider: form.value.primary_provider || undefined,
        fallback_order: fallback.length > 0 ? fallback : undefined,
        provider_config: Object.keys(config).length > 0 ? config : undefined,
        enabled: form.value.enabled,
        priority: form.value.priority || undefined,
      })
      message.success(t('已更新'))
    }
    modalVisible.value = false
    fetchRoutes()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('保存失败'))
  } finally {
    saving.value = false
  }
}

function confirmDelete(r: ModelRoute) {
  Modal.confirm({
    title: t('删除路由规则'),
    content: t('确认删除模型「{id}」的路由规则？', { id: r.model_id }),
    okText: t('删除'),
    okType: 'danger',
    cancelText: t('取消'),
    onOk: async () => {
      try {
        await api.delete(`/v1/ent/model-routes/${r.id}`)
        message.success(t('已删除'))
        fetchRoutes()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('删除失败'))
      }
    },
  })
}

async function toggleEnabled(r: ModelRoute) {
  try {
    await api.put(`/v1/ent/model-routes/${r.id}`, { enabled: !r.enabled })
    r.enabled = !r.enabled
    message.success(r.enabled ? t('已启用') : t('已禁用'))
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('操作失败'))
  }
}

const columns: TableColumnsType = [
  { title: t('模型 ID'), dataIndex: 'model_id', key: 'model_id', width: 180 },
  { title: t('主 Provider'), dataIndex: 'primary_provider', key: 'primary_provider', width: 140 },
  { title: t('降级顺序'), key: 'fallback', width: 160, customRender: ({ record }) => (record.fallback_order ?? []).join(' → ') || '-' },
  { title: t('优先级'), dataIndex: 'priority', key: 'priority', width: 80 },
  { title: t('启用'), key: 'enabled', width: 70, customRender: ({ record }) => record.enabled ? t('是') : t('否') },
  { title: t('更新时间'), dataIndex: 'updated_at', key: 'updated_at', width: 170, customRender: ({ text }) => new Date(text).toLocaleString('zh-CN', { hour12: false }) },
  { title: t('操作'), key: 'action', width: 180, fixed: 'right' },
]

onMounted(fetchRoutes)
</script>

<template>
  <div class="model-router-view">
    <div class="page-header">
      <h2 class="page-title">{{ $t('模型路由管控') }}</h2>
      <a-button type="primary" @click="openCreate">{{ $t('新建路由') }}</a-button>
    </div>

    <a-alert
      type="info"
      show-icon
      :message="$t('按租户配置模型路由：primary_provider 指定首选提供商，fallback_order 定义熔断降级顺序，priority 决定匹配优先级。Python 引擎启动时自动同步。')"
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
          <span class="empty-text">{{ $t('暂无数据') }}</span>
        </div>
      </template>
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'action'">
          <a-button type="link" size="small" @click="toggleEnabled(record as ModelRoute)">
            {{ (record as ModelRoute).enabled ? $t('禁用') : $t('启用') }}
          </a-button>
          <a-button type="link" size="small" @click="openEdit(record as ModelRoute)">{{ $t('编辑') }}</a-button>
          <a-button type="link" size="small" danger @click="confirmDelete(record as ModelRoute)">{{ $t('删除') }}</a-button>
        </template>
      </template>
    </a-table>

    <a-modal
      v-model:open="modalVisible"
      :title="modalMode === 'create' ? $t('新建模型路由') : $t('编辑模型路由')"
      :confirm-loading="saving"
      width="640"
      @ok="save"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('模型 ID')">
          <a-input
            v-model:value="form.model_id"
            :disabled="modalMode === 'edit'"
            :placeholder="$t('例如 gpt-4、claude-3-opus')"
          />
        </a-form-item>
        <a-form-item :label="$t('主 Provider')">
          <a-input
            v-model:value="form.primary_provider"
            :placeholder="$t('例如 openai、anthropic、deepseek')"
          />
        </a-form-item>
        <a-form-item :label="$t('降级顺序（每行一个 provider）')">
          <a-textarea
            v-model:value="form.fallback_order"
            :rows="4"
            class="code-editor"
            placeholder="anthropic&#10;deepseek"
          />
        </a-form-item>
        <a-form-item :label="$t('Provider 配置（JSON）')">
          <a-textarea
            v-model:value="form.provider_config"
            :rows="4"
            class="code-editor"
            placeholder='{"temperature": 0.7, "max_tokens": 4096}'
          />
        </a-form-item>
        <a-form-item :label="$t('优先级')">
          <a-input-number v-model:value="form.priority" :min="1" :max="999" />
        </a-form-item>
        <a-form-item :label="$t('启用')">
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