<script setup lang="ts">
/**
 * 账号注销设置块。
 *
 * 后端状态机：active → pending_deletion（冻结，7 天宽限）→ purged（到期清除）。
 * 三条产品要求必须如实反映在 UI 上：
 *   1. 申请后**只能**导出与撤销（服务端 403 其余全部，前端也要隐藏入口）；
 *   2. 必须显示剩余天数（用户要知道还有多久能改主意）；
 *   3. 撤销要能一键完成，且资产精确恢复。
 *
 * 申请是准不可逆操作，因此要求**勾选确认 + 输入密码**（设过密码的账号）。
 * 不做这些的话，一次误点就会把账号冻结一周。
 */
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { message, Modal } from 'ant-design-vue'
import { WarningOutlined, DownloadOutlined } from '@ant-design/icons-vue'
import {
  getAccountDeletionStatus,
  requestAccountDeletion,
  cancelAccountDeletion,
  downloadMyData,
  type AccountDeletionStatus,
} from '../../api/auth'

const { t } = useI18n()

const loading = ref(false)
const busy = ref(false)
const status = ref<AccountDeletionStatus>({
  status: 'active',
  days_remaining: 0,
  can_export: false,
  can_cancel: false,
})

// 申请流程
const confirming = ref(false)
const confirmed = ref(false)
const password = ref('')

async function refresh() {
  loading.value = true
  try {
    status.value = await getAccountDeletionStatus()
  } catch {
    // 读不到就当正常：不要把设置页整块打红。
    status.value = { status: 'active', days_remaining: 0, can_export: false, can_cancel: false }
  } finally {
    loading.value = false
  }
}

async function handleExport() {
  busy.value = true
  try {
    await downloadMyData()
  } catch {
    message.error(t('auth.export_failed'))
  } finally {
    busy.value = false
  }
}

function askDelete() {
  confirmed.value = false
  password.value = ''
  confirming.value = true
}

async function handleDelete() {
  if (!confirmed.value) {
    message.warning(t('auth.deletion_need_confirm'))
    return
  }
  busy.value = true
  try {
    status.value = await requestAccountDeletion({
      confirm: true,
      password: password.value.trim() || undefined,
    })
    confirming.value = false
    password.value = ''
    message.success(t('auth.deletion_requested'))
  } catch {
    message.error(t('auth.deletion_failed'))
  } finally {
    busy.value = false
  }
}

function handleCancel() {
  // 撤销是恢复性动作，但仍需二次确认：用户可能只是想看看会发生什么。
  Modal.confirm({
    title: t('auth.deletion_cancel_confirm_title'),
    content: t('auth.deletion_cancel_confirm_body'),
    okText: t('auth.deletion_cancel'),
    cancelText: t('common.cancel'),
    onOk: async () => {
      busy.value = true
      try {
        status.value = await cancelAccountDeletion()
        message.success(t('auth.deletion_cancelled'))
      } catch {
        message.error(t('auth.deletion_cancel_failed'))
      } finally {
        busy.value = false
      }
    },
  })
}

onMounted(refresh)
</script>

<template>
  <div class="setting-block">
    <div class="setting-title">
      {{ $t('auth.deletion_title') }}
      <span class="setting-hint">
        <WarningOutlined /> {{ $t('auth.deletion_hint') }}
      </span>
    </div>

    <a-spin :spinning="loading">
      <!-- 冻结中：只留导出与撤销 -->
      <div v-if="status.status === 'pending_deletion'">
        <a-alert
          type="warning"
          show-icon
          :message="$t('auth.deletion_pending_title')"
          :description="$t('auth.deletion_pending_body', { days: status.days_remaining })"
        />
        <a-space class="del-actions">
          <a-button
            :loading="busy"
            @click="handleExport"
          >
            <template #icon>
              <DownloadOutlined />
            </template>
            {{ $t('auth.deletion_export') }}
          </a-button>
          <a-button
            type="primary"
            :loading="busy"
            @click="handleCancel"
          >
            {{ $t('auth.deletion_cancel') }}
          </a-button>
        </a-space>
      </div>

      <!-- 已清除：终态 -->
      <div v-else-if="status.status === 'purged'">
        <a-alert
          type="info"
          show-icon
          :message="$t('auth.deletion_purged')"
        />
      </div>

      <!-- 正常：说明 + 导出 + 申请入口 -->
      <div v-else>
        <a-alert
          type="info"
          show-icon
          :message="$t('auth.deletion_explain')"
        />
        <a-space class="del-actions">
          <a-button
            :loading="busy"
            @click="handleExport"
          >
            <template #icon>
              <DownloadOutlined />
            </template>
            {{ $t('auth.deletion_export') }}
          </a-button>
          <a-button
            v-if="!confirming"
            danger
            @click="askDelete"
          >
            {{ $t('auth.deletion_request') }}
          </a-button>
        </a-space>

        <a-form
          v-if="confirming"
          layout="vertical"
          class="setting-form del-confirm"
        >
          <a-alert
            type="error"
            show-icon
            :message="$t('auth.deletion_warning')"
            class="del-alert"
          />
          <a-form-item>
            <a-checkbox v-model:checked="confirmed">
              {{ $t('auth.deletion_ack') }}
            </a-checkbox>
          </a-form-item>
          <a-form-item :label="$t('auth.current_password')">
            <a-input-password
              v-model:value="password"
              :placeholder="$t('auth.deletion_password_placeholder')"
            />
          </a-form-item>
          <a-form-item>
            <a-space>
              <a-button
                danger
                type="primary"
                :loading="busy"
                @click="handleDelete"
              >
                {{ $t('auth.deletion_request') }}
              </a-button>
              <a-button @click="confirming = false">
                {{ $t('common.cancel') }}
              </a-button>
            </a-space>
          </a-form-item>
        </a-form>
      </div>
    </a-spin>
  </div>
</template>

<style scoped>
.del-actions { margin-top: 12px; }
.del-confirm { margin-top: 16px; }
.del-alert { margin-bottom: 12px; }
</style>
