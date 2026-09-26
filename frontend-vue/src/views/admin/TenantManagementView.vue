<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  Card, Table, Button, Space, Tag, Input, Modal, Form, Select, Drawer,
  Descriptions, Popconfirm, Spin, message,
} from 'ant-design-vue'
import {
  PlusOutlined, EditOutlined, DeleteOutlined, StopOutlined, PlayCircleOutlined,
  BarChartOutlined, ReloadOutlined,
} from '@ant-design/icons-vue'
import { api } from '../../api'
import { apiErrorMessage } from '../../composables/useCrudResource'
import EmptyState from '../../components/common/EmptyState.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
// ── 租户列表 ──
// GET /v1/admin/tenants → { tenants: [{ id, name, status, created_at }] }
const tenants = ref<any[]>([])
const loading = ref(false)

async function loadTenants() {
  loading.value = true
  try {
    const resp = await api.get('/v1/admin/tenants')
    tenants.value = resp.data?.data?.tenants || []
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.failed_to_load_tenant_list')))
  } finally {
    loading.value = false
  }
}

// ── 新建 / 编辑 ──
// POST /v1/admin/tenants { name }; PUT /v1/admin/tenants/{id} { name?, status? }
const modalVisible = ref(false)
const submitting = ref(false)
const editingId = ref<string | null>(null)
const form = ref({ name: '', status: 'active' })

function openCreate() {
  editingId.value = null
  form.value = { name: '', status: 'active' }
  modalVisible.value = true
}

function openEdit(record: any) {
  editingId.value = record.id
  form.value = { name: record.name || '', status: record.status || 'active' }
  modalVisible.value = true
}

async function submitForm() {
  if (!form.value.name.trim()) {
    message.warning(t('admin.please_enter_tenant_name'))
    return
  }
  submitting.value = true
  try {
    if (editingId.value) {
      await api.put(`/v1/admin/tenants/${editingId.value}`, {
        name: form.value.name.trim(),
        status: form.value.status,
      })
      message.success(t('admin.tenant_updated'))
    } else {
      await api.post('/v1/admin/tenants', { name: form.value.name.trim() })
      message.success(t('admin.tenant_created'))
    }
    modalVisible.value = false
    await loadTenants()
  } catch (e: any) {
    message.error(apiErrorMessage(e, editingId.value ? t('errors.failed_to_update_tenant') : t('errors.failed_to_create_tenant')))
  } finally {
    submitting.value = false
  }
}

// ── 挂起 / 恢复 ──
// 挂起：POST /v1/admin/tenants/{id}/suspend；恢复：PUT { status: 'active' }
const togglingId = ref<string | null>(null)

async function toggleSuspend(record: any) {
  togglingId.value = record.id
  try {
    if (record.status === 'suspended') {
      await api.put(`/v1/admin/tenants/${record.id}`, { status: 'active' })
      message.success(t('租户「{name}」已恢复', { name: record.name }))
    } else {
      await api.post(`/v1/admin/tenants/${record.id}/suspend`)
      message.success(t('租户「{name}」已挂起', { name: record.name }))
    }
    await loadTenants()
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.operation_failed')))
  } finally {
    togglingId.value = null
  }
}

// ── 删除 ──
// DELETE /v1/admin/tenants/{id}
async function removeTenant(record: any) {
  try {
    await api.delete(`/v1/admin/tenants/${record.id}`)
    message.success(t('admin.tenant_deleted'))
    await loadTenants()
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.failed_to_delete_tenant')))
  }
}

// ── 用量抽屉 ──
// GET /v1/admin/tenants/{id}/usage → { users, sessions, agent_sessions, knowledge_bases, agents, media_assets }
const usageOpen = ref(false)
const usageLoading = ref(false)
const usage = ref<any>(null)
const usageTenant = ref<any>(null)

const usageItems = [
  { key: 'users', label: t('admin.users') },
  { key: 'sessions', label: t('chat.sessions') },
  { key: 'agent_sessions', label: t('agent.agent_sessions') },
  { key: 'knowledge_bases', label: t('knowledge.knowledge_bases') },
  { key: 'agents', label: t('agent.agents') },
  { key: 'media_assets', label: t('common.media_assets') },
]

async function openUsage(record: any) {
  usageTenant.value = record
  usage.value = null
  usageOpen.value = true
  usageLoading.value = true
  try {
    const resp = await api.get(`/v1/admin/tenants/${record.id}/usage`)
    usage.value = resp.data?.data || {}
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.failed_to_load_usage')))
  } finally {
    usageLoading.value = false
  }
}

// ── 表格列 ──
const columns = [
  { title: t('common.name'), dataIndex: 'name', key: 'name', ellipsis: true },
  { title: t('common.status'), key: 'status', width: 110 },
  { title: t('common.created_at'), key: 'created_at', width: 180 },
  { title: t('common.action'), key: 'actions', width: 260, fixed: 'right' as const },
]

// ── 工具函数 ──
function statusColor(status: string): string {
  return status === 'active' ? 'green' : status === 'suspended' ? 'red' : 'default'
}

function statusText(status: string): string {
  return status === 'active' ? t('common.active') : status === 'suspended' ? t('common.suspended_2') : (status || '-')
}

function formatDate(d: any): string {
  return d ? new Date(d).toLocaleString('zh-CN') : '-'
}

function usageValue(key: string): number {
  const v = usage.value?.[key]
  return typeof v === 'number' ? v : 0
}

onMounted(loadTenants)
</script>

<template>
  <div class="tenant-management">
    <div class="page-header">
      <h1>{{ $t('admin.tenant_management') }}</h1>
      <Space>
        <Button @click="loadTenants">
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('common.refresh') }}
        </Button>
        <Button
          type="primary"
          @click="openCreate"
        >
          <template #icon>
            <PlusOutlined />
          </template>
          {{ $t('admin.new_tenant') }}
        </Button>
      </Space>
    </div>

    <Spin :spinning="loading">
      <Card>
        <Table
          :columns="columns"
          :data-source="tenants"
          row-key="id"
          :pagination="{ pageSize: 20, showSizeChanger: true }"
          :scroll="{ x: 720 }"
        >
          <template #emptyText>
            <EmptyState
              :description="$t('admin.no_tenants_yet')"
              :hint="$t('admin.click_new_tenant_at_the_top_right_to_create_the_first_tenant')"
            />
          </template>

          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'status'">
              <Tag :color="statusColor(record.status)">
                {{ statusText(record.status) }}
              </Tag>
            </template>

            <template v-else-if="column.key === 'created_at'">
              {{ formatDate(record.created_at) }}
            </template>

            <template v-else-if="column.key === 'actions'">
              <Space
                :size="4"
                wrap
              >
                <Button
                  size="small"
                  @click="openUsage(record)"
                >
                  <template #icon>
                    <BarChartOutlined />
                  </template>
                  {{ $t('common.usage') }}
                </Button>
                <Button
                  size="small"
                  @click="openEdit(record)"
                >
                  <template #icon>
                    <EditOutlined />
                  </template>
                  {{ $t('common.edit_2') }}
                </Button>
                <Popconfirm
                  :title="record.status === 'suspended' ? $t('确定恢复租户「{name}」吗？', { name: record.name }) : $t('确定挂起租户「{name}」吗？挂起后其资源将不可用。', { name: record.name })"
                  :ok-text="$t('common.confirm')"
                  :cancel-text="$t('common.cancel')"
                  @confirm="toggleSuspend(record)"
                >
                  <Button
                    size="small"
                    :danger="record.status !== 'suspended'"
                    :loading="togglingId === record.id"
                  >
                    <template #icon>
                      <StopOutlined v-if="record.status !== 'suspended'" />
                      <PlayCircleOutlined v-else />
                    </template>
                    {{ record.status === 'suspended' ? $t('common.restore') : $t('common.suspended') }}
                  </Button>
                </Popconfirm>
                <Popconfirm
                  :title="$t('admin.confirm_deleting_this_tenant_this_action_cannot_be_undone')"
                  :ok-text="$t('common.delete')"
                  :cancel-text="$t('common.cancel')"
                  @confirm="removeTenant(record)"
                >
                  <Button
                    size="small"
                    danger
                  >
                    <template #icon>
                      <DeleteOutlined />
                    </template>
                    {{ $t('common.delete') }}
                  </Button>
                </Popconfirm>
              </Space>
            </template>
          </template>
        </Table>
      </Card>
    </Spin>

    <!-- 新建 / 编辑租户 -->
    <Modal
      v-model:open="modalVisible"
      :title="editingId ? $t('admin.edit_tenant') : $t('admin.new_tenant')"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      :confirm-loading="submitting"
      @ok="submitForm"
    >
      <Form layout="vertical">
        <Form.Item
          :label="$t('admin.tenant_name')"
          required
        >
          <Input
            v-model:value="form.name"
            :placeholder="$t('common.e_g_acme_corporation')"
            @press-enter="submitForm"
          />
        </Form.Item>
        <Form.Item
          v-if="editingId"
          :label="$t('common.status')"
        >
          <Select v-model:value="form.status">
            <Select.Option value="active">
              {{ $t('common.active') }}
            </Select.Option>
            <Select.Option value="suspended">
              {{ $t('common.suspended_2') }}
            </Select.Option>
          </Select>
        </Form.Item>
      </Form>
    </Modal>

    <!-- 用量抽屉 -->
    <Drawer
      v-model:open="usageOpen"
      :title="usageTenant ? $t('📊 {name} - 资源用量', { name: usageTenant.name }) : $t('common.resource_usage')"
      width="420"
      :footer="null"
    >
      <Spin :spinning="usageLoading">
        <Descriptions
          :column="1"
          bordered
          size="small"
        >
          <Descriptions.Item
            v-for="item in usageItems"
            :key="item.key"
            :label="item.label"
          >
            {{ usageValue(item.key) }}
          </Descriptions.Item>
        </Descriptions>
        <div
          v-if="!usageLoading && !usage"
          class="usage-empty"
        >
          <EmptyState :description="$t('common.no_usage_data_yet')" />
        </div>
      </Spin>
    </Drawer>
  </div>
</template>

<style scoped>
.tenant-management { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
.page-header h1 { margin: 0; font-size: 24px; font-weight: 600; color: var(--text-primary); }
.usage-empty { margin-top: 16px; }

/* 窄屏:按钮全宽、触控目标 ≥ 40px */
@media (max-width: 768px) {
  .tenant-management { padding: 16px 12px; }
  .tenant-management .page-header { row-gap: 12px; }
  .tenant-management .page-header .ant-btn { min-height: 40px; }
}
@media (max-width: 480px) {
  .tenant-management .page-header h1 { font-size: 20px; }
}
</style>
