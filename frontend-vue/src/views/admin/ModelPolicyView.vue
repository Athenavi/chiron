<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { message, Modal } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { listModelPolicies, createModelPolicy, updateModelPolicy, deleteModelPolicy } from '../../api/policy'
import type { ModelPolicy } from '../../api/policy'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const loading = ref(false)
const policies = ref<ModelPolicy[]>([])

const modalVisible = ref(false)
const modalMode = ref<'create' | 'edit'>('create')
const form = ref({
  id: '',
  role_id: '',
  allowed_models: '',
  per_model_limits: '{}',
})
const saving = ref(false)

async function fetchPolicies() {
  loading.value = true
  try {
    policies.value = await listModelPolicies()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.failed_to_load'))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  modalMode.value = 'create'
  form.value = { id: '', role_id: '', allowed_models: '', per_model_limits: '{}' }
  modalVisible.value = true
}

function openEdit(p: ModelPolicy) {
  modalMode.value = 'edit'
  form.value = {
    id: p.id,
    role_id: p.role_id ?? '',
    allowed_models: (p.allowed_models ?? []).join('\n'),
    per_model_limits: JSON.stringify(p.per_model_limits ?? {}, null, 2),
  }
  modalVisible.value = true
}

async function save() {
  const models = form.value.allowed_models.split('\n').map(s => s.trim()).filter(Boolean)
  let limits: Record<string, Record<string, number>>
  try {
    limits = JSON.parse(form.value.per_model_limits || '{}')
  } catch {
    message.error(t('agent.per_model_limits_must_be_valid_json'))
    return
  }
  saving.value = true
  try {
    if (modalMode.value === 'create') {
      await createModelPolicy({
        role_id: form.value.role_id || undefined,
        allowed_models: models,
        per_model_limits: limits,
      })
      message.success(t('common.created'))
    } else {
      await updateModelPolicy(form.value.id, {
        role_id: form.value.role_id || undefined,
        allowed_models: models,
        per_model_limits: limits,
      })
      message.success(t('common.updated'))
    }
    modalVisible.value = false
    fetchPolicies()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.save_failed'))
  } finally {
    saving.value = false
  }
}

function confirmDelete(p: ModelPolicy) {
  Modal.confirm({
    title: t('agent.delete_model_policy'),
    content: p.role_id ? t('确认删除角色 {id} 策略？', { id: p.role_id }) : t('admin.confirm_delete_tenant_fallback_policy'),
    okText: t('common.delete'),
    okType: 'danger',
    cancelText: t('common.cancel'),
    onOk: async () => {
      try {
        await deleteModelPolicy(p.id)
        message.success(t('common.deleted'))
        fetchPolicies()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('errors.delete_failed'))
      }
    },
  })
}

function scopeLabel(p: ModelPolicy): string {
  return p.role_id ? t('角色 {id}…', { id: p.role_id.slice(0, 8) }) : t('admin.tenant_level_fallback')
}

const columns: TableColumnsType = [
  { title: t('common.scope'), key: 'scope', width: 160, customRender: ({ record }) => scopeLabel(record) },
  { title: t('agent.allowed_models'), key: 'models', customRender: ({ record }) => (record.allowed_models ?? []).join(', ') || '-' },
  { title: t('agent.model_rate_limit'), key: 'limits', width: 120, customRender: ({ record }) => t('{n} 项', { n: Object.keys(record.per_model_limits ?? {}).length }) },
  { title: t('common.updated_at'), dataIndex: 'updated_at', key: 'updated_at', width: 180, customRender: ({ text }) => new Date(text).toLocaleString('zh-CN', { hour12: false }) },
  { title: t('common.action'), key: 'action', width: 140, fixed: 'right' },
]

onMounted(fetchPolicies)
</script>

<template>
  <div class="policy-view">
    <div class="page-header">
      <h2 class="page-title">
        {{ $t('agent.model_policy_control') }}
      </h2>
      <a-button
        type="primary"
        @click="openCreate"
      >
        {{ $t('admin.new_policy') }}
      </a-button>
    </div>

    <a-alert
      type="info"
      show-icon
      :message="$t('admin.role_specific_exact_policy_takes_precedence_any_hit_from_the_user_s_direct_roles_group_roles_falls_back_to_the_tenant_fallback_when_missing_if_neither_exists_allow')"
      style="margin-bottom: 16px"
    />

    <a-table
      :columns="columns"
      :data-source="policies"
      :loading="loading"
      :row-key="(r: ModelPolicy) => r.id"
      :pagination="false"
      :scroll="{ x: 900 }"
      size="small"
    >
      <template #emptyText>
        <div class="empty-block">
          <span class="empty-icon">📭</span><span class="empty-text">{{ $t('common.no_data_yet') }}</span>
        </div>
      </template>
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'action'">
          <a-button
            type="link"
            size="small"
            @click="openEdit(record as ModelPolicy)"
          >
            {{ $t('common.edit_2') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            danger
            @click="confirmDelete(record as ModelPolicy)"
          >
            {{ $t('common.delete') }}
          </a-button>
        </template>
      </template>
    </a-table>

    <a-modal
      v-model:open="modalVisible"
      :title="modalMode === 'create' ? $t('agent.new_model_policy') : $t('agent.edit_model_policy')"
      :confirm-loading="saving"
      width="640"
      @ok="save"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('admin.role_id_blank_tenant_level_fallback')">
          <a-input
            v-model:value="form.role_id"
            :placeholder="$t('admin.role_uuid_blank_tenant_level')"
          />
        </a-form-item>
        <a-form-item :label="$t('agent.allowed_models_one_model_id_per_line')">
          <a-textarea
            v-model:value="form.allowed_models"
            :rows="6"
            class="code-editor"
            placeholder="gpt-4&#10;claude-3-opus"
          />
        </a-form-item>
        <a-form-item :label="$t('agent.per_model_rate_limit_json')">
          <a-textarea
            v-model:value="form.per_model_limits"
            :rows="5"
            class="code-editor"
            placeholder="{&quot;gpt-4&quot;: {&quot;rpm&quot;: 60}}"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
.policy-view { padding: 16px 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-title { margin: 0; font-size: 20px; }

/* 空状态统一 */
.empty-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 28px 0;
  color: var(--text-tertiary);
}
.empty-icon { font-size: 26px; line-height: 1; opacity: 0.8; }
.empty-text { font-size: 13px; }

/* 代码/JSON 编辑区:等宽字体、min-height 自适应、可纵向拉伸 */
.policy-view :deep(.code-editor) {
  font-family: var(--font-mono);
  font-size: 13px;
  min-height: 120px;
  resize: vertical;
}

/* 窄屏:页头换行、表单项全宽、表格小按钮扩大热区 */
@media (max-width: 768px) {
  .policy-view .page-header { flex-wrap: wrap; row-gap: 12px; }
  .policy-view .page-header .ant-btn { min-height: 40px; }
  .policy-view :deep(.ant-form-item) { margin-bottom: 16px; }
  .policy-view :deep(.ant-btn-sm) { position: relative; }
  .policy-view :deep(.ant-btn-sm)::after {
    content: '';
    position: absolute;
    inset: -8px;
    border-radius: inherit;
  }
}
</style>
