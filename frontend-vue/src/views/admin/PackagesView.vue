<template>
  <div class="packages-admin-view">
    <div class="page-header">
      <div>
        <h2 class="page-title page-title-sm">
          {{ $t('admin.packages_admin_title') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.packages_admin_desc') }}
        </p>
      </div>
      <div class="controls">
        <Button
          :loading="loading"
          @click="load"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('common.refresh') }}
        </Button>
        <Button
          type="primary"
          data-test="package-new"
          @click="openCreate"
        >
          {{ $t('admin.packages_admin_new') }}
        </Button>
      </div>
    </div>

    <Table
      :columns="columns"
      :data-source="rows"
      :loading="loading"
      :pagination="{ pageSize: 20, hideOnSinglePage: true }"
      size="small"
      row-key="id"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'code'">
          <code class="mono">{{ record.code }}</code>
        </template>
        <template v-else-if="column.key === 'price'">
          {{ formatAmount(record.price_cents, record.currency) }}
        </template>
        <template v-else-if="column.key === 'duration'">
          {{ $t('billing.package_validity_days', { n: record.duration_days }) }}
        </template>
        <template v-else-if="column.key === 'models'">
          <!-- 空清单 = 不限制模型，必须显式说明；否则运营会以为"没配"等于"不能用" -->
          <span
            v-if="!record.allowed_models?.length"
            class="sub"
          >{{ $t('billing.package_models_unlimited') }}</span>
          <span
            v-else
            class="sub"
          >{{ record.allowed_models.join(', ') }}</span>
        </template>
        <template v-else-if="column.key === 'status'">
          <Tag :color="record.enabled ? 'success' : 'default'">
            {{ record.enabled
              ? $t('admin.packages_admin_status_on')
              : $t('admin.packages_admin_status_off') }}
          </Tag>
        </template>
        <template v-else-if="column.key === 'actions'">
          <Button
            type="link"
            size="small"
            data-test="package-edit"
            @click="openEdit(record)"
          >
            {{ $t('admin.packages_admin_edit') }}
          </Button>
          <Button
            v-if="record.enabled"
            type="link"
            size="small"
            danger
            data-test="package-disable"
            @click="openDisable(record)"
          >
            {{ $t('admin.packages_admin_disable') }}
          </Button>
          <Button
            v-else
            type="link"
            size="small"
            data-test="package-restore"
            @click="restore(record)"
          >
            {{ $t('admin.packages_admin_restore') }}
          </Button>
        </template>
      </template>
      <template #emptyText>
        <div class="empty">
          {{ $t('admin.packages_admin_empty') }}
        </div>
      </template>
    </Table>

    <Modal
      :open="form.open"
      :title="form.id
        ? $t('admin.packages_admin_edit_title')
        : $t('admin.packages_admin_create_title')"
      :confirm-loading="saving"
      :ok-text="$t('common.save')"
      @ok="submit"
      @cancel="closeForm"
    >
      <div class="form">
        <div class="field">
          <label>{{ $t('admin.packages_admin_code') }}</label>
          <!-- 编辑态禁用：code 是 payments.package_code 的引用值，改了会让
               未确认的订单查不到套餐 —— 钱收得到、权益发不出。后端也会忽略该字段。 -->
          <Input
            v-model:value="form.code"
            :disabled="!!form.id"
            :maxlength="64"
            placeholder="starter"
            data-test="package-code"
          />
          <span
            v-if="!form.id"
            class="hint"
          >{{ $t('admin.packages_admin_code_hint') }}</span>
        </div>

        <div class="field">
          <label>{{ $t('admin.packages_admin_name') }}</label>
          <Input
            v-model:value="form.name"
            :maxlength="128"
            data-test="package-name"
          />
        </div>

        <div class="row">
          <div class="field">
            <label>{{ $t('admin.packages_admin_credits') }}</label>
            <InputNumber
              v-model:value="form.credits"
              :min="0"
              :precision="0"
              style="width: 100%"
              data-test="package-credits"
            />
          </div>
          <div class="field">
            <label>{{ $t('admin.packages_admin_price') }}</label>
            <InputNumber
              v-model:value="form.price_cents"
              :min="0"
              :precision="0"
              style="width: 100%"
              data-test="package-price"
            />
            <span class="hint">{{ $t('admin.packages_admin_price_hint') }}</span>
          </div>
        </div>

        <div class="row">
          <div class="field">
            <label>{{ $t('admin.packages_admin_currency') }}</label>
            <!-- 只给后端真正支持的两种：支付宝/微信收人民币，PayPal 收美元。
                 放行其它币种会让套餐建得成、买不了。 -->
            <Select
              v-model:value="form.currency"
              :options="currencyOptions"
              style="width: 100%"
            />
          </div>
          <div class="field">
            <label>{{ $t('admin.packages_admin_duration') }}</label>
            <InputNumber
              v-model:value="form.duration_days"
              :min="1"
              :precision="0"
              style="width: 100%"
              data-test="package-duration"
            />
          </div>
        </div>

        <div class="field">
          <label>{{ $t('admin.packages_admin_models') }}</label>
          <Select
            v-model:value="form.allowed_models"
            mode="tags"
            :options="modelOptions"
            style="width: 100%"
            data-test="package-models"
          />
          <span class="hint">{{ $t('admin.packages_admin_models_hint') }}</span>
        </div>

        <div class="field">
          <label>{{ $t('admin.packages_admin_sort') }}</label>
          <InputNumber
            v-model:value="form.sort_order"
            :precision="0"
            style="width: 100%"
          />
        </div>
      </div>
    </Modal>

    <Modal
      :open="disableTarget !== null"
      :title="$t('admin.packages_admin_disable')"
      :confirm-loading="saving"
      :ok-button-props="{ danger: true }"
      :ok-text="$t('admin.packages_admin_disable')"
      @ok="confirmDisable"
      @cancel="disableTarget = null"
    >
      <p>{{ $t('admin.packages_admin_disable_hint') }}</p>
      <div
        v-if="disableTarget"
        class="summary"
      >
        <div>{{ disableTarget.name }}</div>
        <div class="sub">
          {{ disableTarget.code }} ·
          {{ formatAmount(disableTarget.price_cents, disableTarget.currency) }}
        </div>
      </div>
    </Modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Button, Input, InputNumber, Modal, Select, Table, Tag, message } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  createAdminPackage,
  disableAdminPackage,
  listAdminPackages,
  restoreAdminPackage,
  updateAdminPackage,
  type AdminPackage,
  type AdminPackageInput,
} from '../../api/admin'
import { listModels } from '../../api/index'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()

const loading = ref(false)
const saving = ref(false)
const rows = ref<AdminPackage[]>([])
const disableTarget = ref<AdminPackage | null>(null)

/** 可选模型清单。取模型发现接口，与用户侧过滤口径同源。 */
const modelOptions = ref<{ value: string; label: string }[]>([])

const currencyOptions = [
  { value: 'CNY', label: 'CNY' },
  { value: 'USD', label: 'USD' },
]

const columns = computed(() => [
  { title: t('admin.packages_admin_col_code'), key: 'code', width: 130 },
  { title: t('admin.packages_admin_col_name'), key: 'name', width: 160 },
  { title: t('admin.packages_admin_col_credits'), key: 'credits', width: 100, align: 'right' as const },
  { title: t('admin.packages_admin_col_price'), key: 'price', width: 110, align: 'right' as const },
  { title: t('admin.packages_admin_col_duration'), key: 'duration', width: 120 },
  { title: t('admin.packages_admin_col_models'), key: 'models' },
  { title: t('admin.packages_admin_col_status'), key: 'status', width: 100 },
  { title: t('admin.packages_admin_col_actions'), key: 'actions', width: 160 },
])

const form = reactive<{
  open: boolean
  id: string
  code: string
  name: string
  credits: number
  price_cents: number
  currency: string
  duration_days: number
  allowed_models: string[]
  sort_order: number
}>({
  open: false,
  id: '',
  code: '',
  name: '',
  credits: 1000,
  price_cents: 1000,
  currency: 'CNY',
  duration_days: 30,
  allowed_models: [],
  sort_order: 0,
})

async function load() {
  loading.value = true
  try {
    rows.value = await listAdminPackages()
  } catch (e) {
    message.error(serverErrorMessage(e, t('common.load_failed')))
  } finally {
    loading.value = false
  }
}

async function loadModels() {
  try {
    const models = await listModels()
    modelOptions.value = (models ?? []).map(m => ({
      value: m.name,
      label: m.display_name ? `${m.display_name} (${m.name})` : m.name,
    }))
  } catch {
    // 模型清单拿不到不影响编辑套餐（可以手工输入模型名），
    // 因此这里静默降级，而不是把整个页面卡住。
    modelOptions.value = []
  }
}

function resetForm() {
  form.id = ''
  form.code = ''
  form.name = ''
  form.credits = 1000
  form.price_cents = 1000
  form.currency = 'CNY'
  form.duration_days = 30
  form.allowed_models = []
  form.sort_order = 0
}

function openCreate() {
  resetForm()
  form.open = true
}

/** antd 的 bodyCell 把 record 声明为宽松类型，这里收窄一次。 */
function openEdit(record: Record<string, any>) {
  const pkg = record as AdminPackage
  form.open = true
  form.id = pkg.id
  form.code = pkg.code
  form.name = pkg.name
  form.credits = pkg.credits
  form.price_cents = pkg.price_cents
  form.currency = pkg.currency || 'CNY'
  form.duration_days = pkg.duration_days
  form.allowed_models = [...(pkg.allowed_models ?? [])]
  form.sort_order = pkg.sort_order ?? 0
}

function closeForm() {
  form.open = false
}

function buildPayload(): AdminPackageInput {
  return {
    code: form.code.trim(),
    name: form.name.trim(),
    credits: Number(form.credits ?? 0),
    price_cents: Number(form.price_cents ?? 0),
    currency: form.currency,
    duration_days: Number(form.duration_days ?? 0),
    allowed_models: (form.allowed_models ?? []).map(s => String(s).trim()).filter(Boolean),
    sort_order: Number(form.sort_order ?? 0),
  }
}

async function submit() {
  const payload = buildPayload()
  // 本地先拦一次明显不合法的组合：后端会返回 422 且带具体原因，
  // 但"没填名字就点保存"这种低级错误不该占用一次往返。
  if (!form.id && !payload.code) {
    message.warning(t('admin.packages_admin_required_hint'))
    return
  }
  if (!payload.name || payload.duration_days <= 0) {
    message.warning(t('admin.packages_admin_required_hint'))
    return
  }

  saving.value = true
  try {
    if (form.id) {
      await updateAdminPackage(form.id, payload)
      message.success(t('admin.packages_admin_updated_msg'))
    } else {
      await createAdminPackage(payload)
      message.success(t('admin.packages_admin_created_msg'))
    }
    closeForm()
    await load()
  } catch (e) {
    // 用后端原文：409（code 重复）与 422（字段不合法）的区别只有原文能表达。
    message.error(serverErrorMessage(e, t('common.save_failed')))
  } finally {
    saving.value = false
  }
}

function openDisable(record: Record<string, any>) {
  disableTarget.value = record as AdminPackage
}

async function confirmDisable() {
  const target = disableTarget.value
  if (!target) return
  saving.value = true
  try {
    await disableAdminPackage(target.id)
    message.success(t('admin.packages_admin_disabled_msg'))
    disableTarget.value = null
    await load()
  } catch (e) {
    message.error(serverErrorMessage(e, t('common.save_failed')))
  } finally {
    saving.value = false
  }
}

async function restore(record: Record<string, any>) {
  const pkg = record as AdminPackage
  try {
    await restoreAdminPackage(pkg.id)
    message.success(t('admin.packages_admin_restored_msg'))
    await load()
  } catch (e) {
    message.error(serverErrorMessage(e, t('common.save_failed')))
  }
}

/** 金额是**最小单位（分）**，直接显示会差 100 倍 */
function formatAmount(cents: number, currency?: string): string {
  const n = Number(cents)
  if (!Number.isFinite(n)) return '-'
  const symbol = !currency || currency === 'CNY' ? '¥' : `${currency} `
  return `${symbol}${(n / 100).toFixed(2)}`
}

onMounted(() => {
  load()
  loadModels()
})
</script>

<style scoped>
.packages-admin-view { padding: 4px; }
.sub { font-size: 12px; color: var(--text-tertiary); }
.mono { font-family: var(--font-mono, monospace); font-size: 12px; }
.empty { padding: 24px 0; color: var(--text-tertiary); }
.form { display: flex; flex-direction: column; gap: 14px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field label { font-weight: var(--fw-medium); color: var(--text-primary); }
.hint { font-size: 12px; color: var(--text-tertiary); }
.summary {
  margin: 12px 0;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--bg-subtle, rgba(0, 0, 0, 0.03));
}
</style>
