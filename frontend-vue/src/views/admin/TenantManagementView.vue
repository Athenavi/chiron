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
    message.error(apiErrorMessage(e, t('加载租户列表失败')))
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
    message.warning(t('请输入租户名称'))
    return
  }
  submitting.value = true
  try {
    if (editingId.value) {
      await api.put(`/v1/admin/tenants/${editingId.value}`, {
        name: form.value.name.trim(),
        status: form.value.status,
      })
      message.success(t('租户已更新'))
    } else {
      await api.post('/v1/admin/tenants', { name: form.value.name.trim() })
      message.success(t('租户已创建'))
    }
    modalVisible.value = false
    await loadTenants()
  } catch (e: any) {
    message.error(apiErrorMessage(e, editingId.value ? t('更新租户失败') : t('创建租户失败')))
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
    message.error(apiErrorMessage(e, t('操作失败')))
  } finally {
    togglingId.value = null
  }
}

// ── 删除 ──
// DELETE /v1/admin/tenants/{id}
async function removeTenant(record: any) {
  try {
    await api.delete(`/v1/admin/tenants/${record.id}`)
    message.success(t('租户已删除'))
    await loadTenants()
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('删除租户失败')))
  }
}

// ── 用量抽屉 ──
// GET /v1/admin/tenants/{id}/usage → { users, sessions, agent_sessions, knowledge_bases, agents, media_assets }
const usageOpen = ref(false)
const usageLoading = ref(false)
const usage = ref<any>(null)
const usageTenant = ref<any>(null)

const usageItems = [
  { key: 'users', label: t('用户数') },
  { key: 'sessions', label: t('会话数') },
  { key: 'agent_sessions', label: t('Agent 会话数') },
  { key: 'knowledge_bases', label: t('知识库数') },
  { key: 'agents', label: t('Agent 数') },
  { key: 'media_assets', label: t('媒体资产数') },
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
    message.error(apiErrorMessage(e, t('加载用量失败')))
  } finally {
    usageLoading.value = false
  }
}

// ── 表格列 ──
const columns = [
  { title: t('名称'), dataIndex: 'name', key: 'name', ellipsis: true },
  { title: t('状态'), key: 'status', width: 110 },
  { title: t('创建时间'), key: 'created_at', width: 180 },
  { title: t('操作'), key: 'actions', width: 260, fixed: 'right' as const },
]

// ── 工具函数 ──
function statusColor(status: string): string {
  return status === 'active' ? 'green' : status === 'suspended' ? 'red' : 'default'
}

function statusText(status: string): string {
  return status === 'active' ? t('活跃') : status === 'suspended' ? t('已挂起') : (status || '-')
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
      <h1>{{ $t('🏢 租户管理') }}</h1>
      <Space>
        <Button @click="loadTenants">
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('刷新') }}
        </Button>
        <Button
          type="primary"
          @click="openCreate"
        >
          <template #icon>
            <PlusOutlined />
          </template>
          {{ $t('新建租户') }}
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
              :description="$t('暂无租户')"
              :hint="$t('点击右上角「新建租户」创建第一个租户')"
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
                  {{ $t('用量') }}
                </Button>
                <Button
                  size="small"
                  @click="openEdit(record)"
                >
                  <template #icon>
                    <EditOutlined />
                  </template>
                  {{ $t('编辑') }}
                </Button>
                <Popconfirm
                  :title="record.status === 'suspended' ? $t('确定恢复租户「{name}」吗？', { name: record.name }) : $t('确定挂起租户「{name}」吗？挂起后其资源将不可用。', { name: record.name })"
                  :ok-text="$t('确定')"
                  :cancel-text="$t('取消')"
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
                    {{ record.status === 'suspended' ? $t('恢复') : $t('挂起') }}
                  </Button>
                </Popconfirm>
                <Popconfirm
                  :title="$t('确定删除该租户吗？此操作不可恢复。')"
                  :ok-text="$t('删除')"
                  :cancel-text="$t('取消')"
                  @confirm="removeTenant(record)"
                >
                  <Button
                    size="small"
                    danger
                  >
                    <template #icon>
                      <DeleteOutlined />
                    </template>
                    {{ $t('删除') }}
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
      :title="editingId ? $t('编辑租户') : $t('新建租户')"
      :ok-text="$t('保存')"
      :cancel-text="$t('取消')"
      :confirm-loading="submitting"
      @ok="submitForm"
    >
      <Form layout="vertical">
        <Form.Item
          :label="$t('租户名称')"
          required
        >
          <Input
            v-model:value="form.name"
            :placeholder="$t('例如: ACME Corporation')"
            @press-enter="submitForm"
          />
        </Form.Item>
        <Form.Item
          v-if="editingId"
          :label="$t('状态')"
        >
          <Select v-model:value="form.status">
            <Select.Option value="active">
              {{ $t('活跃') }}
            </Select.Option>
            <Select.Option value="suspended">
              {{ $t('已挂起') }}
            </Select.Option>
          </Select>
        </Form.Item>
      </Form>
    </Modal>

    <!-- 用量抽屉 -->
    <Drawer
      v-model:open="usageOpen"
      :title="usageTenant ? $t('📊 {name} - 资源用量', { name: usageTenant.name }) : $t('📊 资源用量')"
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
          <EmptyState :description="$t('暂无用量数据')" />
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
