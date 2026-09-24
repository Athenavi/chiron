<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { message, Modal } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import {
  listGroups, createGroup, updateGroup, deleteGroup,
  getGroup, setGroupRoles,
} from '../../api/enterprise'
import { listRoles } from '../../api/enterprise'
import type { EntGroup, EntRole } from '../../api/enterprise'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const loading = ref(false)
const groups = ref<EntGroup[]>([])
const allRoles = ref<EntRole[]>([])

const modalVisible = ref(false)
const modalMode = ref<'create' | 'edit'>('create')
const form = ref({ id: '', name: '', description: '' })
const saving = ref(false)

// 群组角色绑定抽屉
const rolesDrawerVisible = ref(false)
const currentGroup = ref<EntGroup | null>(null)
const selectedRoleIDs = ref<string[]>([])
const rolesSaving = ref(false)

async function fetchGroups() {
  loading.value = true
  try {
    groups.value = await listGroups()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('加载失败'))
  } finally {
    loading.value = false
  }
}

async function fetchRoles() {
  try {
    allRoles.value = await listRoles()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('角色列表加载失败'))
  }
}

function openCreate() {
  modalMode.value = 'create'
  form.value = { id: '', name: '', description: '' }
  modalVisible.value = true
}

function openEdit(g: EntGroup) {
  modalMode.value = 'edit'
  form.value = { id: g.id, name: g.name, description: g.description }
  modalVisible.value = true
}

async function save() {
  if (!form.value.name.trim()) {
    message.warning(t('群组名必填'))
    return
  }
  saving.value = true
  try {
    if (modalMode.value === 'create') {
      await createGroup({ name: form.value.name, description: form.value.description })
      message.success(t('已创建'))
    } else {
      await updateGroup(form.value.id, { name: form.value.name, description: form.value.description })
      message.success(t('已更新'))
    }
    modalVisible.value = false
    fetchGroups()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('保存失败'))
  } finally {
    saving.value = false
  }
}

function confirmDelete(g: EntGroup) {
  Modal.confirm({
    title: t('删除群组'),
    content: `确认删除「${g.name}」？成员关联将解除。`,
    okText: t('删除'),
    okType: 'danger',
    cancelText: t('取消'),
    onOk: async () => {
      try {
        await deleteGroup(g.id)
        message.success(t('已删除'))
        fetchGroups()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('删除失败'))
      }
    },
  })
}

async function openRoleBinding(g: EntGroup) {
  currentGroup.value = g
  try {
    const full = await getGroup(g.id)
    selectedRoleIDs.value = full.role_ids ?? []
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('加载群组角色失败'))
    return
  }
  rolesDrawerVisible.value = true
}

async function saveGroupRoles() {
  if (!currentGroup.value) return
  rolesSaving.value = true
  try {
    await setGroupRoles(currentGroup.value.id, selectedRoleIDs.value)
    message.success(t('已更新群组角色'))
    rolesDrawerVisible.value = false
    fetchGroups()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('保存失败'))
  } finally {
    rolesSaving.value = false
  }
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false })
}

const columns: TableColumnsType = [
  { title: t('群组名'), dataIndex: 'name', key: 'name' },
  { title: t('描述'), dataIndex: 'description', key: 'description', ellipsis: true },
  { title: t('成员数'), dataIndex: 'member_count', key: 'member_count', width: 100 },
  { title: t('创建时间'), dataIndex: 'created_at', key: 'created_at', width: 180, customRender: ({ text }) => formatTime(text) },
  { title: t('操作'), key: 'action', width: 220, fixed: 'right' },
]

onMounted(() => {
  fetchGroups()
  fetchRoles()
})
</script>

<template>
  <div class="groups-view">
    <div class="page-header">
      <h2 class="page-title">
        {{ $t('群组管理') }}
      </h2>
      <a-button
        type="primary"
        @click="openCreate"
      >
        {{ $t('新建群组') }}
      </a-button>
    </div>

    <a-table
      :columns="columns"
      :data-source="groups"
      :loading="loading"
      :row-key="(r: EntGroup) => r.id"
      :pagination="false"
      :scroll="{ x: 860 }"
      size="small"
    >
      <template #emptyText>
        <div class="empty-block">
          <span class="empty-icon">📭</span><span class="empty-text">{{ $t('暂无数据') }}</span>
        </div>
      </template>
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'action'">
          <a-button
            type="link"
            size="small"
            @click="openRoleBinding(record as EntGroup)"
          >
            {{ $t('绑定角色') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            @click="openEdit(record as EntGroup)"
          >
            {{ $t('编辑') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            danger
            @click="confirmDelete(record as EntGroup)"
          >
            {{ $t('删除') }}
          </a-button>
        </template>
      </template>
    </a-table>

    <a-modal
      v-model:open="modalVisible"
      :title="modalMode === 'create' ? '新建群组' : '编辑群组'"
      :confirm-loading="saving"
      @ok="save"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('群组名（唯一，max 128）')">
          <a-input
            v-model:value="form.name"
            :disabled="modalMode === 'edit'"
            :placeholder="$t('如 content-team')"
          />
        </a-form-item>
        <a-form-item :label="$t('描述')">
          <a-textarea
            v-model:value="form.description"
            :rows="3"
            :placeholder="$t('群组用途说明')"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-drawer
      v-model:open="rolesDrawerVisible"
      :title="`群组角色绑定${currentGroup ? ' - ' + currentGroup.name : ''}`"
      width="480"
      placement="right"
    >
      <p class="drawer-hint">
        {{ $t('勾选要授予该群组的角色；群组成员将聚合这些角色的权限点。') }}
      </p>
      <a-checkbox-group
        v-model:value="selectedRoleIDs"
        style="width: 100%"
      >
        <div class="role-list">
          <div
            v-for="r in allRoles"
            :key="r.id"
            class="role-item"
          >
            <a-checkbox :value="r.id">
              <span class="role-name">{{ r.name }}</span>
              <span class="role-desc">{{ r.display_name || r.permissions.join(', ') }}</span>
            </a-checkbox>
          </div>
        </div>
      </a-checkbox-group>
      <template #footer>
        <div style="text-align: right">
          <a-button
            style="margin-right: 8px"
            @click="rolesDrawerVisible = false"
          >
            {{ $t('取消') }}
          </a-button>
          <a-button
            type="primary"
            :loading="rolesSaving"
            @click="saveGroupRoles"
          >
            {{ $t('保存') }}
          </a-button>
        </div>
      </template>
    </a-drawer>
  </div>
</template>

<style scoped>
.groups-view { padding: 16px 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-title { margin: 0; font-size: 20px; }
.drawer-hint { color: var(--text-secondary); font-size: 13px; margin-bottom: 16px; }
.role-list { display: flex; flex-direction: column; gap: 8px; }
.role-item {
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-card);
  transition: background-color var(--dur-normal) ease;
}
.role-item:hover { background: var(--bg-hover); }
.role-name { font-weight: 500; }
.role-desc { margin-left: 8px; color: var(--text-tertiary); font-size: 12px; }

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

/* 窄屏:页头换行、选项说明换行、表格小按钮扩大热区 */
@media (max-width: 768px) {
  .groups-view .page-header { flex-wrap: wrap; row-gap: 12px; }
  .groups-view .page-header .ant-btn { min-height: 40px; }
  .role-desc { display: block; margin-left: 0; margin-top: 2px; }
  .groups-view :deep(.ant-btn-sm) { position: relative; }
  .groups-view :deep(.ant-btn-sm)::after {
    content: '';
    position: absolute;
    inset: -8px;
    border-radius: inherit;
  }
}
</style>
