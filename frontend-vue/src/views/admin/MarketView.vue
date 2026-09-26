<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { message, Modal } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import {
  listMarketItems, createMarketItem, deleteMarketItem,
  publishMarketItem, retireMarketItem, listMarketGrants, grantMarketItem, deleteMarketGrant,
} from '../../api/market'
import type { MarketItem, MarketGrant } from '../../api/market'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const loading = ref(false)
const items = ref<MarketItem[]>([])
const filterType = ref('')
const filterStatus = ref('')

// 条目 Modal
const itemModalVisible = ref(false)
const itemForm = ref({ type: 'skill' as 'plugin' | 'skill', name: '', version: '1.0.0', manifest: '{}' })
const itemSaving = ref(false)

// 授权抽屉
const grantDrawerVisible = ref(false)
const currentItem = ref<MarketItem | null>(null)
const grants = ref<MarketGrant[]>([])
const grantForm = reactive({ tenant_id: '', enabled: true })
const grantSaving = ref(false)

async function fetchItems() {
  loading.value = true
  try {
    items.value = await listMarketItems({
      type: filterType.value || undefined,
      status: filterStatus.value || undefined,
    })
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.failed_to_load'))
  } finally {
    loading.value = false
  }
}

function openCreateItem() {
  itemForm.value = { type: 'skill', name: '', version: '1.0.0', manifest: '{}' }
  itemModalVisible.value = true
}

async function saveItem() {
  if (!itemForm.value.name.trim()) {
    message.warning(t('errors.name_is_required'))
    return
  }
  let manifest: unknown
  try {
    manifest = JSON.parse(itemForm.value.manifest || '{}')
  } catch {
    message.error(t('common.manifest_must_be_valid_json'))
    return
  }
  itemSaving.value = true
  try {
    await createMarketItem({
      type: itemForm.value.type,
      name: itemForm.value.name,
      version: itemForm.value.version,
      manifest,
    })
    message.success(t('common.created_draft'))
    itemModalVisible.value = false
    fetchItems()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.creation_failed'))
  } finally {
    itemSaving.value = false
  }
}

async function publishItem(it: MarketItem) {
  try {
    await publishMarketItem(it.id)
    message.success(t('common.published'))
    fetchItems()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.publish_failed'))
  }
}

async function retireItem(it: MarketItem) {
  Modal.confirm({
    title: t('common.retire_item'),
    content: t('退役「{name}」？退役为终态，不可回 published。', { name: it.name }),
    okText: t('common.retire'),
    okType: 'danger',
    cancelText: t('common.cancel'),
    onOk: async () => {
      try {
        await retireMarketItem(it.id)
        message.success(t('common.retired'))
        fetchItems()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('errors.failed_to_retire'))
      }
    },
  })
}

function confirmDeleteItem(it: MarketItem) {
  Modal.confirm({
    title: t('common.delete_item'),
    content: t('确认删除「{name}」？', { name: it.name }),
    okText: t('common.delete'),
    okType: 'danger',
    cancelText: t('common.cancel'),
    onOk: async () => {
      try {
        await deleteMarketItem(it.id)
        message.success(t('common.deleted'))
        fetchItems()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('errors.delete_failed'))
      }
    },
  })
}

async function openGrantDrawer(it: MarketItem) {
  currentItem.value = it
  grantForm.tenant_id = ''
  grantForm.enabled = true
  try {
    grants.value = await listMarketGrants({ item_id: it.id })
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.failed_to_load_authorization'))
    return
  }
  grantDrawerVisible.value = true
}

async function addGrant() {
  if (!currentItem.value) return
  if (!grantForm.tenant_id.trim()) {
    message.warning(t('errors.tenant_id_is_required'))
    return
  }
  grantSaving.value = true
  try {
    await grantMarketItem({
      item_id: currentItem.value.id,
      tenant_id: grantForm.tenant_id,
      enabled: grantForm.enabled,
    })
    message.success(t('common.authorized'))
    grants.value = await listMarketGrants({ item_id: currentItem.value.id })
    grantForm.tenant_id = ''
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.authorization_failed'))
  } finally {
    grantSaving.value = false
  }
}

async function removeGrant(g: MarketGrant) {
  try {
    await deleteMarketGrant(g.item_id, g.tenant_id)
    message.success(t('common.authorization_revoked'))
    grants.value = grants.value.filter(x => !(x.item_id === g.item_id && x.tenant_id === g.tenant_id))
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.undo_failed'))
  }
}

function statusColor(s: string): string {
  if (s === 'published') return 'green'
  if (s === 'retired') return 'red'
  return 'default'
}

// S 修复：空/非法时间返回 '-'，避免 new Date(null) 抛 RangeError 崩单元格
function formatDateCell(text: any): string {
  if (!text) return '-'
  const d = new Date(text)
  return isNaN(d.getTime()) ? '-' : d.toLocaleString('zh-CN', { hour12: false })
}

const columns: TableColumnsType = [
  { title: t('common.type'), dataIndex: 'type', key: 'type', width: 80 },
  { title: t('common.name'), dataIndex: 'name', key: 'name' },
  { title: t('common.version'), dataIndex: 'version', key: 'version', width: 100 },
  { title: t('common.status'), dataIndex: 'status', key: 'status', width: 100 },
  { title: t('common.updated_at'), dataIndex: 'updated_at', key: 'updated_at', width: 180, customRender: ({ text }) => formatDateCell(text) },
  { title: t('common.action'), key: 'action', width: 280, fixed: 'right' },
]

const grantColumns: TableColumnsType = [
  { title: t('admin.tenant'), dataIndex: 'tenant_id', key: 'tenant_id', ellipsis: true },
  { title: t('common.enable'), dataIndex: 'enabled', key: 'enabled', width: 80, customRender: ({ text }) => (text ? t('common.yes') : t('common.no')) },
  { title: t('common.installed_at'), dataIndex: 'installed_at', key: 'installed_at', width: 180, customRender: ({ text }) => formatDateCell(text) },
  { title: t('common.action'), key: 'action', width: 80, fixed: 'right' },
]

onMounted(fetchItems)
</script>

<template>
  <div class="market-view">
    <div class="page-header">
      <h2 class="page-title">
        {{ $t('common.enterprise_capability_marketplace') }}
      </h2>
      <a-space class="filter-bar">
        <a-select
          v-model:value="filterType"
          :placeholder="$t('common.type')"
          allow-clear
          style="width: 120px"
          @change="fetchItems"
        >
          <a-select-option value="plugin">
            plugin
          </a-select-option>
          <a-select-option value="skill">
            skill
          </a-select-option>
        </a-select>
        <a-select
          v-model:value="filterStatus"
          :placeholder="$t('common.status')"
          allow-clear
          style="width: 140px"
          @change="fetchItems"
        >
          <a-select-option value="draft">
            draft
          </a-select-option>
          <a-select-option value="published">
            published
          </a-select-option>
          <a-select-option value="retired">
            retired
          </a-select-option>
        </a-select>
        <a-button @click="fetchItems">
          {{ $t('common.refresh') }}
        </a-button>
        <a-button
          type="primary"
          @click="openCreateItem"
        >
          {{ $t('common.new_item') }}
        </a-button>
      </a-space>
    </div>

    <a-alert
      type="info"
      show-icon
      :message="$t('admin.state_machine_draft_published_retired_terminal_tenant_installation_records_only_take_effect_when_the_entry_is_published')"
      style="margin-bottom: 16px"
    />

    <a-table
      :columns="columns"
      :data-source="items"
      :loading="loading"
      :row-key="(r: MarketItem) => r.id"
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
        <template v-if="column.key === 'status'">
          <a-tag :color="statusColor(record.status)">
            {{ record.status }}
          </a-tag>
        </template>
        <template v-if="column.key === 'action'">
          <a-button
            type="link"
            size="small"
            @click="openGrantDrawer(record as MarketItem)"
          >
            {{ $t('common.authorize') }}
          </a-button>
          <a-button
            v-if="record.status === 'draft'"
            type="link"
            size="small"
            @click="publishItem(record as MarketItem)"
          >
            {{ $t('common.publish') }}
          </a-button>
          <a-button
            v-if="record.status === 'published'"
            type="link"
            size="small"
            danger
            @click="retireItem(record as MarketItem)"
          >
            {{ $t('common.retire') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            danger
            @click="confirmDeleteItem(record as MarketItem)"
          >
            {{ $t('common.delete') }}
          </a-button>
        </template>
      </template>
    </a-table>

    <a-modal
      v-model:open="itemModalVisible"
      :title="$t('common.new_marketplace_item')"
      :confirm-loading="itemSaving"
      width="640"
      @ok="saveItem"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('common.type')">
          <a-radio-group v-model:value="itemForm.type">
            <a-radio value="plugin">
              plugin
            </a-radio>
            <a-radio value="skill">
              skill
            </a-radio>
          </a-radio-group>
        </a-form-item>
        <a-form-item :label="$t('common.name_unique_max_128')">
          <a-input
            v-model:value="itemForm.name"
            :placeholder="$t('agent.e_g_web_search_skill')"
          />
        </a-form-item>
        <a-form-item :label="$t('common.version')">
          <a-input
            v-model:value="itemForm.version"
            placeholder="1.0.0"
          />
        </a-form-item>
        <a-form-item label="Manifest（JSON）">
          <a-textarea
            v-model:value="itemForm.manifest"
            :rows="6"
            class="code-editor"
            placeholder="{&quot;description&quot;: &quot;...&quot;, &quot;entry&quot;: &quot;...&quot;}"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-drawer
      v-model:open="grantDrawerVisible"
      :title="currentItem ? $t('租户授权 - {name}', { name: currentItem.name }) : $t('admin.tenant_authorization')"
      width="640"
      placement="right"
    >
      <div class="grant-form">
        <a-input
          v-model:value="grantForm.tenant_id"
          :placeholder="$t('admin.tenant_id_uuid')"
          style="flex: 1"
        />
        <a-switch v-model:checked="grantForm.enabled" />
        <span class="hint">{{ $t('common.enable') }}</span>
        <a-button
          type="primary"
          :loading="grantSaving"
          @click="addGrant"
        >
          {{ $t('common.authorize') }}
        </a-button>
      </div>
      <a-table
        :columns="grantColumns"
        :data-source="grants"
        :row-key="(r: MarketGrant) => r.item_id + ':' + r.tenant_id"
        :pagination="false"
        :scroll="{ x: 520 }"
        size="small"
        style="margin-top: 16px"
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
              danger
              @click="removeGrant(record as MarketGrant)"
            >
              {{ $t('common.undo') }}
            </a-button>
          </template>
        </template>
      </a-table>
    </a-drawer>
  </div>
</template>

<style scoped>
.market-view { padding: 16px 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; gap: 12px; }
.page-title { margin: 0; font-size: 20px; }
.grant-form { display: flex; gap: 8px; align-items: center; }
.hint { color: var(--text-secondary); font-size: 12px; }

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

/* JSON 编辑区:等宽字体、min-height 自适应、可纵向拉伸 */
.market-view :deep(.code-editor) {
  font-family: var(--font-mono);
  font-size: 13px;
  min-height: 120px;
  resize: vertical;
}

/* 窄屏:页头/筛选竖排、抽屉授权表单竖排、触控目标 ≥ 40px */
@media (max-width: 768px) {
  .market-view .page-header {
    flex-direction: column;
    align-items: stretch;
  }
  .market-view .filter-bar { width: 100%; }
  .market-view .filter-bar :deep(.ant-space-item) { flex: 1 1 auto; min-width: 0; }
  .market-view .filter-bar :deep(.ant-select) { width: 100% !important; }
  .market-view .filter-bar :deep(.ant-btn) { width: 100%; min-height: 40px; }
  .market-view .grant-form { flex-direction: column; align-items: stretch; }
  .market-view .grant-form :deep(.ant-input),
  .market-view .grant-form :deep(.ant-switch) { width: 100%; }
  .market-view .grant-form :deep(.ant-btn) { width: 100%; min-height: 40px; }
  .market-view :deep(.ant-btn-sm) { position: relative; }
  .market-view :deep(.ant-btn-sm)::after {
    content: '';
    position: absolute;
    inset: -8px;
    border-radius: inherit;
  }
}
</style>
