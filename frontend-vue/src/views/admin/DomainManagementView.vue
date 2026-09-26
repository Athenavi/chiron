<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  Card, Table, Button, Space, Tag, Input, Modal, Form, Alert,
  Descriptions, Popconfirm, Spin, message,
} from 'ant-design-vue'
import {
  PlusOutlined, EditOutlined, DeleteOutlined, CheckCircleOutlined,
  SyncOutlined, ReloadOutlined,
} from '@ant-design/icons-vue'
import { api } from '../../api'
import { apiErrorMessage } from '../../composables/useCrudResource'
import EmptyState from '../../components/common/EmptyState.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
// ── 域名列表 ──
// GET /v1/admin/domains → { domains: [{ id, domain, ssl_status, verified, created_at }] }
const domains = ref<any[]>([])
const loading = ref(false)

async function loadDomains() {
  loading.value = true
  try {
    const resp = await api.get('/v1/admin/domains')
    domains.value = resp.data?.data?.domains || []
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.failed_to_load_domain_list')))
  } finally {
    loading.value = false
  }
}

// ── 新建 / 编辑 ──
// POST /v1/admin/domains { domain }; PUT /v1/admin/domains/{id} { domain }
const modalVisible = ref(false)
const submitting = ref(false)
const editingId = ref<string | null>(null)
const form = ref({ domain: '' })

function openCreate() {
  editingId.value = null
  form.value = { domain: '' }
  modalVisible.value = true
}

function openEdit(record: any) {
  editingId.value = record.id
  form.value = { domain: record.domain || '' }
  modalVisible.value = true
}

async function submitForm() {
  if (!form.value.domain.trim()) {
    message.warning(t('admin.please_enter_domain'))
    return
  }
  submitting.value = true
  try {
    if (editingId.value) {
      await api.put(`/v1/admin/domains/${editingId.value}`, { domain: form.value.domain.trim() })
      message.success(t('admin.domain_updated'))
    } else {
      await api.post('/v1/admin/domains', { domain: form.value.domain.trim() })
      message.success(t('admin.domain_added'))
    }
    modalVisible.value = false
    await loadDomains()
  } catch (e: any) {
    message.error(apiErrorMessage(e, editingId.value ? t('errors.failed_to_update_domain') : t('errors.failed_to_add_domain')))
  } finally {
    submitting.value = false
  }
}

// ── 验证 ──
// POST /v1/admin/domains/{id}/verify → { verified, addresses?, reason? }
const verifyVisible = ref(false)
const verifyResult = ref<any>(null)
const verifyingId = ref<string | null>(null)

async function verifyDomain(record: any) {
  verifyingId.value = record.id
  try {
    const resp = await api.post(`/v1/admin/domains/${record.id}/verify`)
    verifyResult.value = resp.data?.data || {}
    verifyVisible.value = true
    await loadDomains()
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('auth.domain_verification_failed')))
  } finally {
    verifyingId.value = null
  }
}

// ── SSL 续期 ──
// POST /v1/admin/domains/{id}/renew-ssl → { ssl_status, note? }
const renewingId = ref<string | null>(null)

async function renewSSL(record: any) {
  renewingId.value = record.id
  try {
    const resp = await api.post(`/v1/admin/domains/${record.id}/renew-ssl`)
    const d = resp.data?.data || {}
    message.success(d.note ? t('SSL 续期完成（{status}）：{note}', { status: d.ssl_status || 'ok', note: d.note }) : t('SSL 续期完成（{status}）', { status: d.ssl_status || 'ok' }))
    await loadDomains()
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.ssl_renewal_failed')))
  } finally {
    renewingId.value = null
  }
}

// ── 删除 ──
// DELETE /v1/admin/domains/{id}
async function removeDomain(record: any) {
  try {
    await api.delete(`/v1/admin/domains/${record.id}`)
    message.success(t('admin.domain_deleted'))
    await loadDomains()
  } catch (e: any) {
    message.error(apiErrorMessage(e, t('errors.failed_to_delete_domain')))
  }
}

// ── 表格列 ──
const columns = [
  { title: t('admin.domain'), dataIndex: 'domain', key: 'domain', ellipsis: true },
  { title: t('auth.verification_status'), key: 'verified', width: 110 },
  { title: t('common.ssl_status'), key: 'ssl_status', width: 120 },
  { title: t('common.created_at'), key: 'created_at', width: 180 },
  { title: t('common.action'), key: 'actions', width: 280, fixed: 'right' as const },
]

// ── 工具函数 ──
function sslStatusText(status: string): string {
  switch (status) {
    case 'active': return t('common.valid')
    case 'pending': return t('common.issuing')
    case 'expired': return t('errors.expired')
    case 'failed': return t('errors.failed')
    default: return status || '-'
  }
}

function sslStatusColor(status: string): string {
  switch (status) {
    case 'active': return 'green'
    case 'pending': return 'orange'
    case 'expired': return 'red'
    case 'failed': return 'red'
    default: return 'default'
  }
}

function formatDate(d: any): string {
  return d ? new Date(d).toLocaleString('zh-CN') : '-'
}

function formatAddresses(a: any): string {
  if (a == null) return '-'
  if (Array.isArray(a)) return a.join('；')
  if (typeof a === 'object') return Object.entries(a).map(([k, v]) => `${k}: ${v}`).join('；')
  return String(a)
}

onMounted(loadDomains)
</script>

<template>
  <div class="domain-management">
    <div class="page-header">
      <h1>{{ $t('admin.domain_management') }}</h1>
      <Space>
        <Button @click="loadDomains">
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
          {{ $t('admin.add_domain') }}
        </Button>
      </Space>
    </div>

    <Alert
      :message="$t('admin.domain_setup_guide')"
      :description="$t('admin.after_adding_a_domain_click_verify_to_get_the_dns_records_to_configure_after_configuring_them_at_your_dns_provider_click_verify_again_to_confirm_before_expiry_click_renew_to_refresh_the_ssl_certificate')"
      type="info"
      show-icon
      style="margin-bottom: 16px"
    />

    <Spin :spinning="loading">
      <Card>
        <Table
          :columns="columns"
          :data-source="domains"
          row-key="id"
          :pagination="{ pageSize: 20, showSizeChanger: true }"
          :scroll="{ x: 900 }"
        >
          <template #emptyText>
            <EmptyState
              :description="$t('admin.no_domains_yet')"
              :hint="$t('admin.click_add_domain_at_the_top_right_to_connect_your_first_domain')"
            />
          </template>

          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'verified'">
              <Tag :color="record.verified ? 'green' : 'orange'">
                {{ record.verified ? $t('common.verified') : $t('common.unverified') }}
              </Tag>
            </template>

            <template v-else-if="column.key === 'ssl_status'">
              <Tag :color="sslStatusColor(record.ssl_status)">
                {{ sslStatusText(record.ssl_status) }}
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
                  :loading="verifyingId === record.id"
                  @click="verifyDomain(record)"
                >
                  <template #icon>
                    <CheckCircleOutlined />
                  </template>
                  {{ $t('common.verify') }}
                </Button>
                <Button
                  size="small"
                  :loading="renewingId === record.id"
                  @click="renewSSL(record)"
                >
                  <template #icon>
                    <SyncOutlined />
                  </template>
                  {{ $t('common.renew_ssl') }}
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
                  :title="$t('admin.confirm_deleting_this_domain_its_ssl_certificate_and_access_config_will_be_removed_as_well')"
                  :ok-text="$t('common.delete')"
                  :cancel-text="$t('common.cancel')"
                  @confirm="removeDomain(record)"
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

    <!-- 验证结果 -->
    <Modal
      v-model:open="verifyVisible"
      :title="$t('auth.domain_verification_result')"
      :footer="null"
      :width="560"
      @cancel="verifyResult = null"
    >
      <template v-if="verifyResult">
        <Alert
          :type="verifyResult.verified ? 'success' : 'error'"
          :message="verifyResult.verified ? $t('admin.verified_domain_is_connected') : $t('auth.verification_failed')"
          show-icon
          style="margin-bottom: 16px"
        />
        <Descriptions
          v-if="verifyResult.verified"
          :column="1"
          bordered
          size="small"
        >
          <Descriptions.Item :label="$t('common.resolve_address')">
            {{ formatAddresses(verifyResult.addresses) }}
          </Descriptions.Item>
        </Descriptions>
        <Alert
          v-else-if="verifyResult.reason"
          type="warning"
          :message="String(verifyResult.reason)"
          show-icon
        />
      </template>
    </Modal>

    <!-- 新建 / 编辑域名 -->
    <Modal
      v-model:open="modalVisible"
      :title="editingId ? $t('admin.edit_domain') : $t('admin.add_domain')"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      :confirm-loading="submitting"
      @ok="submitForm"
    >
      <Form layout="vertical">
        <Form.Item
          :label="$t('admin.domain')"
          required
        >
          <Input
            v-model:value="form.domain"
            placeholder="example.com"
            @press-enter="submitForm"
          />
        </Form.Item>
      </Form>
    </Modal>
  </div>
</template>

<style scoped>
.domain-management { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
.page-header h1 { margin: 0; font-size: 24px; font-weight: 600; color: var(--text-primary); }

@media (max-width: 768px) {
  .domain-management { padding: 16px 12px; }
  .domain-management .page-header { row-gap: 12px; }
  .domain-management .page-header .ant-btn { min-height: 40px; }
}
@media (max-width: 480px) {
  .domain-management .page-header h1 { font-size: 20px; }
}
</style>
