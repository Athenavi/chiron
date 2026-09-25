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
    message.error(e?.response?.data?.error || t('加载失败'))
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
    message.warning(t('名称必填'))
    return
  }
  let manifest: unknown
  try {
    manifest = JSON.parse(itemForm.value.manifest || '{}')
  } catch {
    message.error(t('manifest 必须是合法 JSON'))
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
    message.success(t('已创建（draft）'))
    itemModalVisible.value = false
    fetchItems()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('创建失败'))
  } finally {
    itemSaving.value = false
  }
}

async function publishItem(it: MarketItem) {
  try {
    await publishMarketItem(it.id)
    message.success(t('已发布'))
    fetchItems()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('发布失败'))
  }
}

async function retireItem(it: MarketItem) {
  Modal.confirm({
    title: t('退役条目'),
    content: t('退役「{name}」？退役为终态，不可回 published。', { name: it.name }),
    okText: t('退役'),
    okType: 'danger',
    cancelText: t('取消'),
    onOk: async () => {
      try {
        await retireMarketItem(it.id)
        message.success(t('已退役'))
        fetchItems()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('退役失败'))
      }
    },
  })
}

function confirmDeleteItem(it: MarketItem) {
  Modal.confirm({
    title: t('删除条目'),
    content: t('确认删除「{name}」？', { name: it.name }),
    okText: t('删除'),
    okType: 'danger',
    cancelText: t('取消'),
    onOk: async () => {
      try {
        await deleteMarketItem(it.id)
        message.success(t('已删除'))
        fetchItems()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('删除失败'))
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
    message.error(e?.response?.data?.error || t('加载授权失败'))
    return
  }
  grantDrawerVisible.value = true
}

async function addGrant() {
  if (!currentItem.value) return
  if (!grantForm.tenant_id.trim()) {
    message.warning(t('租户 ID 必填'))
    return
  }
  grantSaving.value = true
  try {
    await grantMarketItem({
      item_id: currentItem.value.id,
      tenant_id: grantForm.tenant_id,
      enabled: grantForm.enabled,
    })
    message.success(t('已授权'))
    grants.value = await listMarketGrants({ item_id: currentItem.value.id })
    grantForm.tenant_id = ''
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('授权失败'))
  } finally {
    grantSaving.value = false
  }
}

async function removeGrant(g: MarketGrant) {
  try {
    await deleteMarketGrant(g.item_id, g.tenant_id)
    message.success(t('已撤销授权'))
    grants.value = grants.value.filter(x => !(x.item_id === g.item_id && x.tenant_id === g.tenant_id))
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('撤销失败'))
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
  { title: t('类型'), dataIndex: 'type', key: 'type', width: 80 },
  { title: t('名称'), dataIndex: 'name', key: 'name' },
  { title: t('版本'), dataIndex: 'version', key: 'version', width: 100 },
  { title: t('状态'), dataIndex: 'status', key: 'status', width: 100 },
  { title: t('更新时间'), dataIndex: 'updated_at', key: 'updated_at', width: 180, customRender: ({ text }) => formatDateCell(text) },
  { title: t('操作'), key: 'action', width: 280, fixed: 'right' },
]

const grantColumns: TableColumnsType = [
  { title: t('租户'), dataIndex: 'tenant_id', key: 'tenant_id', ellipsis: true },
  { title: t('启用'), dataIndex: 'enabled', key: 'enabled', width: 80, customRender: ({ text }) => (text ? t('是') : t('否')) },
  { title: t('安装时间'), dataIndex: 'installed_at', key: 'installed_at', width: 180, customRender: ({ text }) => formatDateCell(text) },
  { title: t('操作'), key: 'action', width: 80, fixed: 'right' },
]

onMounted(fetchItems)
</script>

<template>
  <div class="market-view">
    <div class="page-header">
      <h2 class="page-title">
        {{ $t('企业能力市场') }}
      </h2>
      <a-space class="filter-bar">
        <a-select
          v-model:value="filterType"
          :placeholder="$t('类型')"
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
          :placeholder="$t('状态')"
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
          {{ $t('刷新') }}
        </a-button>
        <a-button
          type="primary"
          @click="openCreateItem"
        >
          {{ $t('新建条目') }}
        </a-button>
      </a-space>
    </div>

    <a-alert
      type="info"
      show-icon
      :message="$t('状态机：draft → published → retired（终态）。租户安装记录须条目为 published 才生效。')"
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
          <span class="empty-icon">📭</span><span class="empty-text">{{ $t('暂无数据') }}</span>
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
            {{ $t('授权') }}
          </a-button>
          <a-button
            v-if="record.status === 'draft'"
            type="link"
            size="small"
            @click="publishItem(record as MarketItem)"
          >
            {{ $t('发布') }}
          </a-button>
          <a-button
            v-if="record.status === 'published'"
            type="link"
            size="small"
            danger
            @click="retireItem(record as MarketItem)"
          >
            {{ $t('退役') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            danger
            @click="confirmDeleteItem(record as MarketItem)"
          >
            {{ $t('删除') }}
          </a-button>
        </template>
      </template>
    </a-table>

    <a-modal
      v-model:open="itemModalVisible"
      :title="$t('新建市场条目')"
      :confirm-loading="itemSaving"
      width="640"
      @ok="saveItem"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('类型')">
          <a-radio-group v-model:value="itemForm.type">
            <a-radio value="plugin">
              plugin
            </a-radio>
            <a-radio value="skill">
              skill
            </a-radio>
          </a-radio-group>
        </a-form-item>
        <a-form-item :label="$t('名称（唯一，max 128）')">
          <a-input
            v-model:value="itemForm.name"
            :placeholder="$t('如 web-search-skill')"
          />
        </a-form-item>
        <a-form-item :label="$t('版本')">
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
      :title="currentItem ? $t('租户授权 - {name}', { name: currentItem.name }) : $t('租户授权')"
      width="640"
      placement="right"
    >
      <div class="grant-form">
        <a-input
          v-model:value="grantForm.tenant_id"
          :placeholder="$t('租户 ID（UUID）')"
          style="flex: 1"
        />
        <a-switch v-model:checked="grantForm.enabled" />
        <span class="hint">{{ $t('启用') }}</span>
        <a-button
          type="primary"
          :loading="grantSaving"
          @click="addGrant"
        >
          {{ $t('授权') }}
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
            <span class="empty-icon">📭</span><span class="empty-text">{{ $t('暂无数据') }}</span>
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
              {{ $t('撤销') }}
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
