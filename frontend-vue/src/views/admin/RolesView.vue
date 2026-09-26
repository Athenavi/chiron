<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { message, Modal } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { listRoles, createRole, updateRole, deleteRole } from '../../api/enterprise'
import type { EntRole } from '../../api/enterprise'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const loading = ref(false)
const roles = ref<EntRole[]>([])

const modalVisible = ref(false)
const modalMode = ref<'create' | 'edit'>('create')
const form = ref({ id: '', name: '', display_name: '', permissions: '' })
const saving = ref(false)

async function fetchRoles() {
  loading.value = true
  try {
    roles.value = await listRoles()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.failed_to_load'))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  modalMode.value = 'create'
  form.value = { id: '', name: '', display_name: '', permissions: '' }
  modalVisible.value = true
}

function openEdit(role: EntRole) {
  modalMode.value = 'edit'
  form.value = {
    id: role.id,
    name: role.name,
    display_name: role.display_name,
    permissions: role.permissions.join('\n'),
  }
  modalVisible.value = true
}

async function save() {
  if (!form.value.name.trim()) {
    message.warning(t('errors.role_name_is_required'))
    return
  }
  const perms = form.value.permissions
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean)
  saving.value = true
  try {
    if (modalMode.value === 'create') {
      await createRole({ name: form.value.name, display_name: form.value.display_name, permissions: perms })
      message.success(t('common.created'))
    } else {
      await updateRole(form.value.id, { name: form.value.name, display_name: form.value.display_name, permissions: perms })
      message.success(t('common.updated'))
    }
    modalVisible.value = false
    fetchRoles()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.save_failed'))
  } finally {
    saving.value = false
  }
}

function confirmDelete(role: EntRole) {
  if (role.is_builtin) {
    message.warning(t('admin.built_in_roles_cannot_be_deleted'))
    return
  }
  Modal.confirm({
    title: t('admin.delete_role'),
    content: t('确认删除「{name}」？关联用户将失去此角色权限。', { name: role.name }),
    okText: t('common.delete'),
    okType: 'danger',
    cancelText: t('common.cancel'),
    onOk: async () => {
      try {
        await deleteRole(role.id)
        message.success(t('common.deleted'))
        fetchRoles()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('errors.delete_failed'))
      }
    },
  })
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false })
}

const columns: TableColumnsType = [
  { title: t('admin.role_name'), dataIndex: 'name', key: 'name' },
  { title: t('common.display_name'), dataIndex: 'display_name', key: 'display_name' },
  { title: t('common.built_in'), dataIndex: 'is_builtin', key: 'is_builtin', width: 80, customRender: ({ text }) => (text ? t('common.yes') : t('common.no')) },
  { title: t('admin.users'), dataIndex: 'user_count', key: 'user_count', width: 80 },
  { title: t('errors.permission_point'), dataIndex: 'permissions', key: 'permissions', customRender: ({ text }) => (text as string[])?.length ?? 0 },
  { title: t('common.created_at'), dataIndex: 'created_at', key: 'created_at', width: 180, customRender: ({ text }) => formatTime(text) },
  { title: t('common.action'), key: 'action', width: 140, fixed: 'right' },
]

onMounted(fetchRoles)
</script>

<template>
  <div class="roles-view">
    <div class="page-header">
      <h2 class="page-title">
        {{ $t('admin.role_management') }}
      </h2>
      <a-button
        type="primary"
        @click="openCreate"
      >
        {{ $t('admin.new_role') }}
      </a-button>
    </div>

    <a-table
      :columns="columns"
      :data-source="roles"
      :loading="loading"
      :row-key="(r: EntRole) => r.id"
      :pagination="false"
      :scroll="{ x: 860 }"
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
            @click="openEdit(record as EntRole)"
          >
            {{ $t('common.edit_2') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            danger
            @click="confirmDelete(record as EntRole)"
          >
            {{ $t('common.delete') }}
          </a-button>
        </template>
      </template>
    </a-table>

    <a-modal
      v-model:open="modalVisible"
      :title="modalMode === 'create' ? $t('admin.new_role') : $t('admin.edit_role')"
      :confirm-loading="saving"
      @ok="save"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('admin.role_name_unique_max_64')">
          <a-input
            v-model:value="form.name"
            :disabled="modalMode === 'edit'"
            :placeholder="$t('common.e_g_content_editor_2')"
          />
        </a-form-item>
        <a-form-item :label="$t('common.display_name')">
          <a-input
            v-model:value="form.display_name"
            :placeholder="$t('common.e_g_content_editor')"
          />
        </a-form-item>
        <a-form-item :label="$t('errors.permission_points_one_per_line')">
          <a-textarea
            v-model:value="form.permissions"
            :rows="8"
            :placeholder="$t('billing.e_g_ent_manage_naudit_read_nbilling_manage')"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
.roles-view { padding: 16px 24px; }
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

/* 文本编辑区:min-height 自适应、可纵向拉伸 */
.roles-view :deep(textarea.ant-input) {
  min-height: 160px;
  resize: vertical;
}

/* 窄屏:页头换行、表格小按钮扩大热区 */
@media (max-width: 768px) {
  .roles-view .page-header { flex-wrap: wrap; row-gap: 12px; }
  .roles-view .page-header .ant-btn { min-height: 40px; }
  .roles-view :deep(.ant-btn-sm) { position: relative; }
  .roles-view :deep(.ant-btn-sm)::after {
    content: '';
    position: absolute;
    inset: -8px;
    border-radius: inherit;
  }
}
</style>
