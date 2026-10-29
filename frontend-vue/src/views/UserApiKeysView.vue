<script setup lang="ts">
/**
 * 用户级 API Key 自助管理（P1-3 / T2）。
 *
 * 与 `views/admin/ApiKeysView.vue` **不是同一个东西**：那个页面管的是 LLM 供应商
 * 密钥池（`/v1/admin/api-keys`，带 provider / base_url / weight），属于平台运营配置，
 * 普通用户不该看到。本页打的是 `/v1/api-keys`，管的是「我自己的账号能用来调网关的
 * 凭证」，越权由后端 SQL 里的 `user_id = $1` 挡住。
 *
 * 两条硬约束决定了这个页面的形态：
 *
 * 1. **明文只出现一次**。后端只在 POST 响应里给 `key`，之后列表永远不含它，库里
 *    只有 sha256。所以创建成功后必须停在弹窗里让用户复制，关掉就等于永久丢失 ——
 *    这是页面最重要的一条交互，不能被「创建成功」的 toast 一笔带过。
 * 2. **已吊销的 key 保留展示**。用户需要看到「这把是我上周停掉的」，否则会以为
 *    自己从没创建过，进而重复创建。
 */
import { ref, computed, onMounted } from 'vue'
import { Table, Button, Input, Modal, Tag, message, Popconfirm, Alert, Spin } from 'ant-design-vue'
import { KeyOutlined, PlusOutlined, CopyOutlined } from '@ant-design/icons-vue'
import dayjs from 'dayjs'
import EmptyState from '../components/common/EmptyState.vue'
import {
  listUserApiKeys,
  createUserApiKey,
  revokeUserApiKey,
  type UserAPIKey,
} from '../api/userApiKeys'
import { useI18n } from 'vue-i18n'
import { errorMessageText } from '../utils/apiError'

const { t } = useI18n()

const keys = ref<UserAPIKey[]>([])
const loading = ref(false)

// ── 创建 ──
const createOpen = ref(false)
const createName = ref('')
const creating = ref(false)
/** 创建成功后拿到的明文。非空时弹窗切换成「请立刻复制」形态，不再可提交。 */
const createdPlain = ref('')
const copied = ref(false)

const nameValid = computed(() => createName.value.trim().length > 0)

function openCreate() {
  createName.value = ''
  createdPlain.value = ''
  copied.value = false
  createOpen.value = true
}

/**
 * 关闭创建弹窗。
 *
 * 明文还没被复制就关闭要二次确认 —— 关掉之后后端再也给不出这把 key 的明文，
 * 用户只能吊销重建。这个拦截的成本很低，但能避免一次真正的麻烦。
 */
function closeCreate() {
  if (createdPlain.value && !copied.value) {
    Modal.confirm({
      title: t('settings.user_api_key_close_unconfirmed_title'),
      content: t('settings.user_api_key_close_unconfirmed_body'),
      okText: t('settings.user_api_key_close_anyway'),
      cancelText: t('common.cancel'),
      onOk: () => { createOpen.value = false },
    })
    return
  }
  createOpen.value = false
}

async function submitCreate() {
  if (!nameValid.value || creating.value) return
  creating.value = true
  try {
    const created = await createUserApiKey(createName.value.trim())
    createdPlain.value = created.key
    copied.value = false
    message.success(t('settings.user_api_key_created'))
    // 不在这里关弹窗：明文要留在屏幕上等用户复制
    await loadKeys()
  } catch (e) {
    message.error(errorMessageText(e, t('settings.user_api_key_create_failed')))
  } finally {
    creating.value = false
  }
}

async function copyPlain() {
  try {
    await navigator.clipboard.writeText(createdPlain.value)
    copied.value = true
    message.success(t('settings.user_api_key_copied'))
  } catch {
    // 剪贴板不可用（非 https / 无权限）时不要假装成功：让用户自己选中复制
    message.warning(t('settings.user_api_key_copy_failed'))
  }
}

// ── 列表 ──

async function loadKeys() {
  loading.value = true
  try {
    keys.value = await listUserApiKeys()
  } catch (e) {
    message.error(errorMessageText(e, t('settings.user_api_key_load_failed')))
  } finally {
    loading.value = false
  }
}

async function revoke(id: string) {
  try {
    await revokeUserApiKey(id)
    message.success(t('settings.user_api_key_revoked_msg'))
    await loadKeys()
  } catch (e) {
    message.error(errorMessageText(e, t('settings.user_api_key_revoke_failed')))
  }
}

function formatTime(v?: string): string {
  return v ? dayjs(v).format('YYYY-MM-DD HH:mm') : ''
}

// 列标题要在 computed 里取 t()：直接写在 const 里会固化首次渲染的语言。
const columns = computed(() => [
  { title: t('settings.user_api_key_col_name'), dataIndex: 'name', key: 'name' },
  { title: t('settings.user_api_key_col_created'), dataIndex: 'created_at', key: 'created_at', width: 180 },
  { title: t('settings.user_api_key_col_last_used'), dataIndex: 'last_used_at', key: 'last_used_at', width: 180 },
  { title: t('settings.user_api_key_col_status'), dataIndex: 'revoked', key: 'revoked', width: 110 },
  { title: t('settings.user_api_key_col_action'), key: 'action', width: 110 },
])

onMounted(loadKeys)
</script>

<template>
  <div class="keys-container">
    <div class="keys-header">
      <KeyOutlined style="font-size: 26px; color: var(--primary)" />
      <h1>{{ $t('settings.user_api_key_title') }}</h1>
    </div>

    <Alert
      type="info"
      :message="$t('settings.user_api_key_desc')"
      show-icon
      style="margin-bottom: 16px"
    />

    <div class="keys-toolbar">
      <Button
        type="primary"
        data-test="user-key-create"
        @click="openCreate"
      >
        <template #icon>
          <PlusOutlined />
        </template>
        {{ $t('settings.user_api_key_create') }}
      </Button>
    </div>

    <div
      v-if="loading && keys.length === 0"
      class="keys-loading"
    >
      <Spin />
    </div>

    <Table
      v-else-if="keys.length > 0"
      :columns="columns"
      :data-source="keys"
      row-key="id"
      :pagination="false"
      size="middle"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'name'">
          <code class="key-name">{{ record.name }}</code>
        </template>
        <template v-else-if="column.key === 'created_at'">
          <span class="key-time">{{ formatTime(record.created_at) || '—' }}</span>
        </template>
        <template v-else-if="column.key === 'last_used_at'">
          <!-- 后端目前从不回写这一列，如实显示「从未使用」而不是留空 ——
               空单元格会被读成「加载失败」 -->
          <span class="key-time">
            {{ formatTime(record.last_used_at) || $t('settings.user_api_key_never_used') }}
          </span>
        </template>
        <template v-else-if="column.key === 'revoked'">
          <Tag :color="record.revoked ? 'default' : 'success'">
            {{ record.revoked ? $t('settings.user_api_key_status_revoked') : $t('settings.user_api_key_status_active') }}
          </Tag>
        </template>
        <template v-else-if="column.key === 'action'">
          <Popconfirm
            v-if="!record.revoked"
            :title="$t('settings.user_api_key_revoke_confirm')"
            :ok-text="$t('common.confirm')"
            :cancel-text="$t('common.cancel')"
            @confirm="revoke(record.id)"
          >
            <Button
              type="link"
              danger
              size="small"
              :data-test="`user-key-revoke-${record.id}`"
            >
              {{ $t('settings.user_api_key_revoke') }}
            </Button>
          </Popconfirm>
          <span
            v-else
            class="key-time"
          >—</span>
        </template>
      </template>
    </Table>

    <EmptyState
      v-else
      :description="$t('settings.user_api_key_empty')"
      :hint="$t('settings.user_api_key_empty_hint')"
    />

    <Modal
      v-model:open="createOpen"
      :title="createdPlain ? $t('settings.user_api_key_created') : $t('settings.user_api_key_create')"
      :footer="null"
      :mask-closable="false"
      @cancel="closeCreate"
    >
      <!-- 阶段一：填名字 -->
      <template v-if="!createdPlain">
        <div class="create-field">
          <label class="create-label">{{ $t('settings.user_api_key_name') }}</label>
          <Input
            v-model:value="createName"
            :placeholder="$t('settings.user_api_key_name_placeholder')"
            :maxlength="128"
            data-test="user-key-name"
            @press-enter="submitCreate"
          />
        </div>
        <div class="create-actions">
          <Button @click="closeCreate">
            {{ $t('common.cancel') }}
          </Button>
          <Button
            type="primary"
            :loading="creating"
            :disabled="!nameValid"
            data-test="user-key-submit"
            @click="submitCreate"
          >
            {{ $t('settings.user_api_key_create') }}
          </Button>
        </div>
      </template>

      <!-- 阶段二：明文只此一次 -->
      <template v-else>
        <Alert
          type="warning"
          :message="$t('settings.user_api_key_created_hint')"
          show-icon
          style="margin-bottom: 12px"
        />
        <div class="plain-key">
          <code data-test="user-key-plain">{{ createdPlain }}</code>
          <Button
            size="small"
            data-test="user-key-copy"
            @click="copyPlain"
          >
            <template #icon>
              <CopyOutlined />
            </template>
            {{ copied ? $t('settings.user_api_key_copied') : $t('settings.user_api_key_copy') }}
          </Button>
        </div>
        <div class="create-actions">
          <Button
            type="primary"
            data-test="user-key-done"
            @click="closeCreate"
          >
            {{ $t('settings.user_api_key_done') }}
          </Button>
        </div>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
.keys-container {
  padding: 24px;
  max-width: 1080px;
  margin: 0 auto;
}

.keys-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}

.keys-header h1 {
  margin: 0;
  font-size: 24px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}

.keys-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}

.keys-loading {
  display: flex;
  justify-content: center;
  padding: 48px 0;
}

.key-name {
  font-family: var(--font-mono, monospace);
  font-size: 13px;
}

.key-time {
  font-size: 13px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.create-field {
  margin-bottom: 16px;
}

.create-label {
  display: block;
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--text-secondary);
}

.create-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}

.plain-key {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--bg-secondary, rgba(0, 0, 0, 0.04));
  word-break: break-all;
}

.plain-key code {
  flex: 1;
  font-family: var(--font-mono, monospace);
  font-size: 13px;
}

@media (max-width: 640px) {
  .keys-container { padding: 16px 12px; }
  .keys-header h1 { font-size: 20px; }
  .plain-key { flex-direction: column; align-items: flex-start; }
}
</style>
