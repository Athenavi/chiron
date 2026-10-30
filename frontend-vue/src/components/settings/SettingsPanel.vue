<script setup lang="ts">
/**
 * 用户设置面板：通用 / 账户 / 数据 / 协议四个标签页，外加仅在桌面壳出现的
 * 「运行模式」与「本地模型」两个设备级页签。
 *
 * 页面（/profile）与头像弹窗共用本组件 —— 两套入口一套实现，避免各自漂移。
 * 组件本身不含容器（无 header、无尺寸），由调用方决定是页面还是弹窗。
 */
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { Button, Input, Form, FormItem, message, Popconfirm, Tag, Spin, Slider } from 'ant-design-vue'
import { SafetyOutlined, MobileOutlined, ExportOutlined, DeleteOutlined, LinkOutlined } from '@ant-design/icons-vue'
import EmptyState from '../common/EmptyState.vue'
import HarnessProfilePanel from './HarnessProfilePanel.vue'
import RunModePanel from './RunModePanel.vue'
import LocalModelPanel from './LocalModelPanel.vue'
import LanguageSwitcher from '../common/LanguageSwitcher.vue'
import { useAuthStore } from '../../stores/auth'
import { useThemeStore } from '../../stores/theme'
import { useDeviceStore } from '../../stores/device'
import { api } from '../../api'
import {
  setPassword,
} from '../../api/auth'
import { useSettingsBindings } from '../../composables/useSettingsBindings'
import { useSettingsConversations } from '../../composables/useSettingsConversations'
import { useSettingsPhone } from '../../composables/useSettingsPhone'
import { useSettingsShares } from '../../composables/useSettingsShares'
import { useSpeech } from '../../composables/useSpeech'
import { ACCENT_PRESETS, DEFAULT_ACCENT } from '../../utils/accentPresets'
import { serverErrorMessage } from '../../utils/apiError'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const props = withDefaults(defineProps<{ initialTab?: string }>(), { initialTab: 'general' })

const route = useRoute()
const authStore = useAuthStore()
const themeStore = useThemeStore()
// 运行模式只在桌面壳里可切（Web 构建是纯云模式），据此决定是否展示该标签页
const deviceStore = useDeviceStore()

const activeTab = ref(props.initialTab)

// ── 账户资料 ──

const loading = ref(false)
const form = ref({ name: '', email: '' })

async function handleUpdateProfile() {
  loading.value = true
  try {
    await api.put('/v1/auth/profile', form.value)
    message.success(t('settings.profile_updated'))
    await authStore.fetchProfile()
  } catch (error) {
    message.error((error instanceof Error ? error.message : '') || t('errors.update_failed'))
  } finally {
    loading.value = false
  }
}

// ── 三方账号绑定：状态与逻辑见 `composables/useSettingsBindings.ts` ──
const {
  identities,
  bindable,
  bindingsLoading,
  unbinding,
  loadBindings,
  openBindings,
  startBind,
  handleUnbind,
} = useSettingsBindings()

// ── 手机号绑定（短信验证码）：状态与逻辑见 `composables/useSettingsPhone.ts` ──
const {
  phone,
  phoneBound,
  phoneLoading,
  phoneForm,
  phoneBinding,
  unbindingPhone,
  sendingCode,
  phoneCountdown,
  loadPhone,
  handleSendBindCode,
  handleBindPhone,
  handleUnbindPhone,
} = useSettingsPhone()

// ── 设置密码（SSO 建号用户首设免旧密码）──

const pwdForm = ref({ current_password: '', new_password: '', confirm: '' })
const pwdLoading = ref(false)

async function handleSetPassword() {
  const { current_password, new_password, confirm } = pwdForm.value
  if (new_password.length < 8 || new_password.length > 128) {
    message.warning(t('auth.new_password_must_be_8_128_characters'))
    return
  }
  if (new_password !== confirm) {
    message.warning(t('auth.the_two_new_password_entries_do_not_match'))
    return
  }
  pwdLoading.value = true
  try {
    await setPassword({ current_password: current_password || undefined, new_password })
    message.success(t('auth.password_set'))
    pwdForm.value = { current_password: '', new_password: '', confirm: '' }
  } catch (e) {
    const apiErr = serverErrorMessage(e, '')
    if (apiErr === 'current_password is required') {
      message.error(t('auth.this_account_already_has_a_password_please_enter_the_current_password_first'))
    } else if (apiErr === 'invalid current password') {
      message.error(t('auth.current_password_is_incorrect'))
    } else {
      message.error(apiErr || t('errors.failed_to_set'))
    }
  } finally {
    pwdLoading.value = false
  }
}

// ── 朗读音色 ──
// 走浏览器原生 speechSynthesis（见 composables/useSpeech.ts）；偏好写回后端
// users.settings.tts，以便换设备后仍是同一套设置。音色本体来自本机系统，
// 换机器后同名音色可能不存在，此时 useSpeech 会回退到系统默认。

const { supported: ttsSupported, voicesByLang, speak, stop, speaking } = useSpeech()

const speech = ref({ voiceURI: '', rate: 1, pitch: 1 })
const speechSaving = ref(false)

function loadSpeechPrefs() {
  const saved = authStore.user?.settings?.tts as
    | { voiceURI?: unknown; rate?: unknown; pitch?: unknown }
    | undefined
  if (!saved) return
  speech.value = {
    voiceURI: typeof saved.voiceURI === 'string' ? saved.voiceURI : '',
    rate: typeof saved.rate === 'number' ? saved.rate : 1,
    pitch: typeof saved.pitch === 'number' ? saved.pitch : 1,
  }
}

async function saveSpeechPrefs() {
  speechSaving.value = true
  try {
    await api.put('/v1/auth/profile', { settings: { tts: speech.value } })
    message.success(t('settings.read_aloud_settings_saved'))
    await authStore.fetchProfile()
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.save_failed')))
  } finally {
    speechSaving.value = false
  }
}

function previewSpeech() {
  if (!speak(t('common.this_is_a_text_to_speech_preview'), speech.value)) {
    message.warning(t('common.please_select_a_voice_first_or_the_current_browser_does_not_support_read_aloud'))
  }
}

// ── 数据管理：分享：状态与逻辑见 `composables/useSettingsShares.ts` ──
const {
  shares,
  sharesLoading,
  revoking,
  loadShares,
  openShares,
  handleRevokeShare,
  copyShareURL,
} = useSettingsShares()

// ── 数据管理：会话导出与删除：状态与逻辑见 `composables/useSettingsConversations.ts` ──
// 删除会话会让分享列表失效，故把 `loadShares` 注进去（两者互不 import）。
const {
  conversations,
  convLoading,
  selectedConvIds,
  exporting,
  deleting,
  allSelected,
  selectedCount,
  loadConversations,
  openConversations,
  exportSelected,
  deleteSelected,
  toggleSelectAll,
} = useSettingsConversations({ onSessionsDeleted: loadShares })

// ── 生命周期 ──

// 延后中的统计请求：卸载时逐个取消，否则一个已经被关掉的弹窗还会在 1.5 秒后开口
let cancelTally: Array<() => void> = []

onMounted(async () => {
  if (authStore.user) {
    form.value.name = authStore.user.name || ''
    form.value.email = authStore.user.email || ''
  }
  // 绑定成功回跳（bindURL=/profile?bind=ok）；弹窗入口不会带该参数
  if (route.query.bind === 'ok') {
    message.success(t('auth.third_party_account_bound_successfully'))
  }
  loadSpeechPrefs()
  // 本地模式没有租户/三方绑定/短信/会话分享与会话列表接口（本地后端第一批没实现），
  // 不做这几次必然 501 的请求：与其弹一堆"本地模式暂不支持"，不如让这些云侧页签留空。
  if (deviceStore.isLocal()) return

  // 打开面板即触发的这几次只读请求，逐个按**成本**决定是否接进延后 + TTL 缓存：
  //
  // · `GET /v1/conversations`（列表）——**昂贵**，走缓存。`List` 之后是
  //   `session.Manager.ListSessions`：`SELECT … FROM sessions WHERE user_id = $1
  //   ORDER BY pinned DESC, updated_at DESC LIMIT $2`，**没有 `(user_id, updated_at)`
  //   索引**（基线 0001 只建了 `ix_sessions_parent_session_id` / `ix_sessions_tag`），
  //   也就是每个用户每一次都得对全表按 user_id 过滤再排序；再叠一次父会话标题批量查询
  //   （`parentTitlesFor` → `ParentTitles`）。会话数越多的用户越慢，且它与用户是否真去
  //   看「数据管理」页签完全无关。
  // · `GET /v1/shares`（分享管理）——**昂贵**，走缓存。`ShareHandler.List` 是
  //   `SELECT … FROM conversation_shares WHERE user_id = $1 AND revoked_at IS NULL
  //   AND expires_at > NOW() ORDER BY created_at DESC LIMIT 200`，同样**没有 user_id 索引**
  //   （基线只有 `conversation_shares_pkey`），扫完还要 `array_length` 聚合每行的消息数。
  // · `GET /v1/auth/sso/identities`（三方绑定）——**昂贵**，走缓存：它 JOIN
  //   `ent_user_identities` 与 `ent_oidc_providers` 且 `ent_user_identities` 上
  //   **只有主键索引**（基线 0001 的索引清单里没有 user_id 上的任何索引），按 user_id 过滤即扫表。
  //   与它打包的 `GET /v1/auth/sso/providers`（可绑定列表）是 `ent_oidc_providers` 按
  //   `tenant_id` 的小查询、数量个位数，本身很便宜 —— 但它只随上面的身份列表一起用，
  //   跟着同一份缓存值走，不额外发就是最优，不值得为它单独做一套。
  //
  // **明确排除**：`GET /v1/auth/sms/bind`（`loadPhone`）。`SmsHandler.GetBind` 是
  //   `SELECT phone FROM users WHERE id = $1` —— 主键单行，是这批里最便宜的一个，
  //   缓存它只会带来"刚绑定完还显示未绑定"的风险（而它必须随账户页签即时反映）。
  //   所以它保持原样：打开面板即发、不延后、不缓存。
  cancelTally = [
    openBindings(),
    openShares(),
    openConversations(),
  ]
  loadPhone()
})

onUnmounted(() => {
  cancelTally.forEach(cancel => cancel())
  cancelTally = []
})
</script>

<template>
  <a-tabs v-model:active-key="activeTab">
    <!-- ── 通用设置 ── -->
    <a-tab-pane
      key="general"
      :tab="$t('settings.general_settings')"
    >
      <div class="setting-block">
        <div class="setting-title">
          {{ $t('settings.appearance_and_theme') }}
        </div>
        <div class="appearance-row">
          <div class="appearance-label">
            {{ $t('common.light_dark_mode') }}
          </div>
          <a-radio-group
            :value="themeStore.preference"
            @change="() => themeStore.toggleTheme()"
          >
            <a-radio-button :value="'dark'">
              {{ $t('common.dark') }}
            </a-radio-button>
            <a-radio-button :value="'light'">
              {{ $t('common.light') }}
            </a-radio-button>
          </a-radio-group>
        </div>
        <div class="appearance-row">
          <div class="appearance-label">
            {{ $t('common.accent_color') }}
          </div>
          <div class="accent-picker">
            <button
              v-for="c in ACCENT_PRESETS"
              :key="c"
              type="button"
              class="accent-swatch"
              :class="{ active: themeStore.accent === c }"
              :style="{ backgroundColor: c }"
              :title="c"
              @click="themeStore.setAccent(c)"
            />
            <label
              class="accent-custom"
              :style="{ backgroundColor: themeStore.accent || DEFAULT_ACCENT }"
              :title="$t('common.custom_color')"
            >
              <input
                type="color"
                :value="themeStore.accent"
                @input="(e: any) => themeStore.setAccent(e.target.value)"
              >
            </label>
          </div>
        </div>
        <div class="appearance-row">
          <div class="appearance-label">
            {{ $t('common.language') }}
          </div>
          <LanguageSwitcher />
        </div>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('common.read_aloud') }}
          <span class="setting-hint">{{ $t('common.use_system_voice_no_network_needed') }}</span>
        </div>
        <EmptyState
          v-if="!ttsSupported"
          :description="$t('common.current_browser_does_not_support_speech_synthesis')"
        />
        <template v-else>
          <div class="appearance-row">
            <div class="appearance-label">
              {{ $t('common.voice') }}
            </div>
            <a-select
              v-model:value="speech.voiceURI"
              style="min-width: 240px; flex: 1"
              show-search
              option-filter-prop="label"
              :placeholder="$t('common.system_default')"
              allow-clear
            >
              <a-select-opt-group
                v-for="[lang, list] in voicesByLang"
                :key="lang"
                :label="lang"
              >
                <a-select-option
                  v-for="v in list"
                  :key="v.voiceURI"
                  :value="v.voiceURI"
                  :label="v.localService ? v.name : $t('common.name_online', { name: v.name })"
                >
                  {{ v.name }}<span v-if="!v.localService">{{ $t('common.online') }}</span>
                </a-select-option>
              </a-select-opt-group>
            </a-select>
          </div>
          <div class="appearance-row">
            <div class="appearance-label">
              {{ $t('common.speed') }}
            </div>
            <Slider
              v-model:value="speech.rate"
              :min="0.5"
              :max="2"
              :step="0.1"
              style="flex: 1; min-width: 200px"
            />
            <span class="setting-hint">{{ speech.rate.toFixed(1) }}x</span>
          </div>
          <div class="appearance-row">
            <div class="appearance-label">
              {{ $t('common.pitch') }}
            </div>
            <Slider
              v-model:value="speech.pitch"
              :min="0"
              :max="2"
              :step="0.1"
              style="flex: 1; min-width: 200px"
            />
            <span class="setting-hint">{{ speech.pitch.toFixed(1) }}</span>
          </div>
          <div class="appearance-row">
            <Button
              :disabled="speaking"
              @click="previewSpeech"
            >
              {{ $t('common.preview_voice') }}
            </Button>
            <Button
              v-if="speaking"
              danger
              @click="stop"
            >
              {{ $t('common.stop') }}
            </Button>
            <Button
              type="primary"
              :loading="speechSaving"
              @click="saveSpeechPrefs"
            >
              {{ $t('settings.save_settings') }}
            </Button>
          </div>
        </template>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('common.language') }}
          <span class="setting-hint">{{ $t('common.coming_soon') }}</span>
        </div>
        <EmptyState :description="$t('common.the_multilingual_ui_is_in_development_currently_simplified_chinese_only')" />
      </div>
    </a-tab-pane>

    <!-- ── 账户设置 ── -->
    <a-tab-pane
      key="account"
      :tab="$t('auth.account_settings')"
    >
      <div class="setting-block">
        <div class="setting-title">
          {{ $t('settings.basic_profile') }}
        </div>
        <Form
          :model="form"
          layout="vertical"
          class="setting-form"
        >
          <FormItem :label="$t('auth.username')">
            <Input
              v-model:value="form.name"
              :placeholder="$t('auth.please_enter_username')"
            />
          </FormItem>
          <FormItem :label="$t('auth.email')">
            <Input
              v-model:value="form.email"
              :placeholder="$t('mail.please_enter_email')"
              disabled
            />
          </FormItem>
          <FormItem>
            <Button
              type="primary"
              :loading="loading"
              @click="handleUpdateProfile"
            >
              {{ $t('common.save_changes') }}
            </Button>
          </FormItem>
        </Form>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('auth.third_party_account_binding') }}
        </div>
        <Spin :spinning="bindingsLoading">
          <EmptyState
            v-if="!identities.length && !bindable.length"
            :description="$t('auth.no_available_third_party_login_methods_yet')"
          />
          <template v-else>
            <div
              v-for="item in identities"
              :key="item.id"
              class="identity-row"
            >
              <div class="identity-info">
                <Tag color="blue">
                  {{ item.provider_type }}
                </Tag>
                <span class="identity-name">{{ item.provider_name }}</span>
                <span class="identity-meta">{{ item.email || item.subject }}</span>
              </div>
              <Popconfirm
                :title="$t('auth.confirm_unbinding_this_third_party_account')"
                :ok-text="$t('common.unbind')"
                :cancel-text="$t('common.cancel')"
                @confirm="handleUnbind(item.id)"
              >
                <Button
                  danger
                  :loading="unbinding === item.id"
                >
                  {{ $t('common.unbind') }}
                </Button>
              </Popconfirm>
            </div>
            <div
              v-if="bindable.length"
              class="bind-section"
            >
              <div class="bind-title">
                {{ $t('auth.bindable_third_party_accounts') }}
              </div>
              <div class="bind-buttons">
                <Button
                  v-for="p in bindable"
                  :key="p.id"
                  @click="startBind(p.id)"
                >
                  {{ $t('common.bind_name', { name: p.display_name || p.name }) }}
                </Button>
              </div>
            </div>
          </template>
        </Spin>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('auth.phone') }}
        </div>
        <Spin :spinning="phoneLoading">
          <div
            v-if="phoneBound"
            class="identity-row"
          >
            <div class="identity-info">
              <Tag color="green">
                <MobileOutlined />
              </Tag>
              <span class="identity-name">{{ phone }}</span>
              <span class="identity-meta">{{ $t('auth.can_be_used_for_sms_verification_login') }}</span>
            </div>
            <Popconfirm
              :title="$t('common.confirm_unbinding_this_phone_number')"
              :ok-text="$t('common.unbind')"
              :cancel-text="$t('common.cancel')"
              @confirm="handleUnbindPhone"
            >
              <Button
                danger
                :loading="unbindingPhone"
              >
                {{ $t('common.unbind') }}
              </Button>
            </Popconfirm>
          </div>
          <Form
            v-else
            :model="phoneForm"
            layout="vertical"
            class="setting-form"
          >
            <FormItem :label="$t('auth.phone')">
              <Input
                v-model:value="phoneForm.phone"
                :placeholder="$t('common.please_enter_phone_number')"
                :maxlength="21"
              >
                <template #prefix>
                  <MobileOutlined />
                </template>
              </Input>
            </FormItem>
            <FormItem :label="$t('auth.verificationCode')">
              <Input
                v-model:value="phoneForm.code"
                :placeholder="$t('auth.sms_verification_code')"
                :maxlength="6"
              >
                <template #suffix>
                  <Button
                    size="small"
                    type="link"
                    :disabled="phoneCountdown > 0 || sendingCode"
                    :loading="sendingCode"
                    @click="handleSendBindCode"
                  >
                    {{ phoneCountdown > 0 ? $t('common.resend_in_n_s', { n: phoneCountdown }) : $t('auth.get_verification_code') }}
                  </Button>
                </template>
              </Input>
            </FormItem>
            <FormItem>
              <Button
                type="primary"
                :loading="phoneBinding"
                @click="handleBindPhone"
              >
                {{ $t('common.bind_phone_number') }}
              </Button>
            </FormItem>
          </Form>
        </Spin>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('auth.set_password') }}
          <span class="setting-hint"><SafetyOutlined /> {{ $t('auth.third_party_account_setting_a_password_for_the_first_time_does_not_require_the_current_password') }}</span>
        </div>
        <Form
          :model="pwdForm"
          layout="vertical"
          class="setting-form"
        >
          <FormItem :label="$t('auth.current_password_leave_empty_on_first_set')">
            <Input
              v-model:value="pwdForm.current_password"
              type="password"
              :placeholder="$t('auth.required_for_accounts_that_have_set_a_password')"
            />
          </FormItem>
          <FormItem :label="$t('auth.new_password')">
            <Input
              v-model:value="pwdForm.new_password"
              type="password"
              :placeholder="$t('common.8_128_characters')"
            />
          </FormItem>
          <FormItem :label="$t('auth.confirm_new_password')">
            <Input
              v-model:value="pwdForm.confirm"
              type="password"
              :placeholder="$t('auth.enter_the_new_password_again')"
            />
          </FormItem>
          <FormItem>
            <Button
              type="primary"
              :loading="pwdLoading"
              @click="handleSetPassword"
            >
              {{ $t('auth.save_password') }}
            </Button>
          </FormItem>
        </Form>
      </div>
    </a-tab-pane>

    <!-- ── 数据管理 ── -->
    <a-tab-pane
      key="data"
      :tab="$t('common.data_management')"
    >
      <div class="setting-block">
        <div class="setting-title">
          {{ $t('common.share_management') }}
          <span class="setting-hint">{{ $t('errors.published_conversation_canceling_makes_the_link_invalid_immediately') }}</span>
        </div>
        <Spin :spinning="sharesLoading">
          <EmptyState
            v-if="!shares.length"
            :description="$t('chat.no_conversations_shared_yet')"
          />
          <div
            v-for="item in shares"
            :key="item.share_id"
            class="identity-row"
          >
            <div class="identity-info">
              <LinkOutlined class="row-icon" />
              <span class="identity-name">{{ item.title || $t('chat.untitled_conversation') }}</span>
              <span class="identity-meta">
                {{ $t('chat.n_messages_time', { n: item.message_count, time: new Date(item.created_at).toLocaleString() }) }}
              </span>
            </div>
            <div class="row-actions">
              <Button
                size="small"
                @click="copyShareURL(item.share_id)"
              >
                {{ $t('common.copy_link') }}
              </Button>
              <Popconfirm
                :title="$t('errors.confirm_canceling_this_share_the_link_will_become_invalid_immediately')"
                :ok-text="$t('common.cancel_sharing')"
                :cancel-text="$t('common.back')"
                @confirm="handleRevokeShare(item)"
              >
                <Button
                  size="small"
                  danger
                  :loading="revoking === item.share_id"
                >
                  {{ $t('common.cancel_sharing') }}
                </Button>
              </Popconfirm>
            </div>
          </div>
        </Spin>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('chat.history_sessions') }}
          <span class="setting-hint">
            {{ $t('common.selected_sel_total', { sel: selectedCount, total: conversations.length }) }}
          </span>
        </div>
        <div class="data-toolbar">
          <Button
            size="small"
            @click="toggleSelectAll"
          >
            {{ allSelected ? $t('common.clear_selection') : $t('common.select_all') }}
          </Button>
          <Button
            size="small"
            :disabled="!selectedCount"
            :loading="exporting"
            @click="exportSelected"
          >
            <ExportOutlined /> {{ $t('common.export_selected_markdown') }}
          </Button>
          <Popconfirm
            :title="$t('chat.confirm_deleting_the_selected_sessions_this_action_cannot_be_undone')"
            :ok-text="$t('common.delete')"
            :cancel-text="$t('common.cancel')"
            @confirm="deleteSelected"
          >
            <Button
              size="small"
              danger
              :disabled="!selectedCount"
              :loading="deleting"
            >
              <DeleteOutlined /> {{ $t('common.delete_selected') }}
            </Button>
          </Popconfirm>
        </div>
        <Spin :spinning="convLoading">
          <div class="conv-list">
            <label
              v-for="c in conversations"
              :key="c.id"
              class="conv-row"
            >
              <a-checkbox
                :checked="selectedConvIds.includes(c.id)"
                @change="(e: any) => {
                  selectedConvIds = e.target.checked
                    ? [...selectedConvIds, c.id]
                    : selectedConvIds.filter(x => x !== c.id)
                }"
              />
              <span class="conv-title">{{ c.title || $t('chat.untitled_session') }}</span>
              <span class="conv-meta">{{ c.updated_at ? new Date(c.updated_at).toLocaleString() : '' }}</span>
            </label>
            <EmptyState
              v-if="!conversations.length"
              :description="$t('chat.no_history_sessions_yet')"
            />
          </div>
        </Spin>
      </div>
    </a-tab-pane>

    <!-- ── 服务协议 ── -->
    <a-tab-pane
      key="legal"
      :tab="$t('common.terms_of_service')"
    >
      <div class="setting-block">
        <div class="setting-title">
          {{ $t('admin.user_agreement') }}
        </div>
        <div class="legal-body">
          <p>{{ $t('common.welcome_to_chiron_before_using_this_service_please_read_and_agree_to_the_following_terms') }}</p>
          <p><strong>{{ $t('common.1_service_scope') }}</strong>{{ $t('workflow.this_service_provides_llm_based_conversation_knowledge_base_and_workflow_orchestration_service_may_be_interrupted_for_maintenance_upgrades_or_force_majeure') }}</p>
          <p><strong>{{ $t('auth.2_account_responsibility') }}</strong>{{ $t('auth.you_must_safeguard_your_account_credentials_and_are_responsible_for_all_activity_under_your_account_unauthorized_use_must_be_reported_to_us_immediately') }}</p>
          <p><strong>{{ $t('common.3_usage_rules') }}</strong>{{ $t('admin.do_not_use_this_service_to_generate_or_spread_illegal_content_or_to_attack_scrape_or_reverse_engineer_in_ways_that_harm_the_service_or_other_users') }}</p>
          <p><strong>{{ $t('common.4_content_ownership') }}</strong>{{ $t('common.your_input_belongs_to_you_using_generated_content_must_comply_with_applicable_law_you_are_responsible_for_content_you_publish') }}</p>
          <p><strong>{{ $t('common.5_changes_termination') }}</strong>{{ $t('settings.we_may_adjust_or_terminate_some_features_major_changes_will_be_announced_in_advance') }}</p>
        </div>
      </div>
      <div class="setting-block">
        <div class="setting-title">
          {{ $t('admin.privacy_policy') }}
        </div>
        <div class="legal-body">
          <p><strong>{{ $t('common.1_data_collected') }}</strong>{{ $t('auth.we_collect_account_info_email_username_the_conversations_and_knowledge_base_content_you_submit_and_necessary_access_logs') }}</p>
          <p><strong>{{ $t('common.2_purpose') }}</strong>{{ $t('common.used_to_provide_the_service_ensure_security_and_improve_the_product_not_used_for_unrelated_purposes') }}</p>
          <p><strong>{{ $t('common.3_storage_protection') }}</strong>{{ $t('common.data_is_stored_on_your_deployed_instance_we_apply_access_control_and_transport_encryption_measures') }}</p>
          <p><strong>{{ $t('common.4_third_party_sharing') }}</strong>{{ $t('errors.except_as_required_by_law_or_explicitly_authorized_by_you_we_do_not_share_your_personal_data_with_third_parties') }}</p>
          <p><strong>{{ $t('common.5_your_rights') }}</strong>{{ $t('auth.you_can_export_or_delete_history_sessions_in_data_management_and_unbind_third_party_accounts_or_phone_numbers_in_account_settings') }}</p>
          <p><strong>{{ $t('common.6_contact') }}</strong>{{ $t('admin.if_you_have_questions_about_this_policy_contact_us_via_the_channel_provided_by_the_deployer') }}</p>
        </div>
      </div>
    </a-tab-pane>

    <!-- ── 行为塑形（P1-④ 批 B）：会被追加到 system prompt 末尾，必须显著告知 ── -->
    <a-tab-pane
      key="harness"
      :tab="$t('settings.harness_title')"
    >
      <HarnessProfilePanel scope="user" />
    </a-tab-pane>

    <!-- ── 运行模式（local / cloud）：仅桌面壳展示；切换代价与确认在面板内告知 ── -->
    <a-tab-pane
      v-if="deviceStore.isDesktop"
      key="mode"
      :tab="$t('settings.run_mode')"
    >
      <RunModePanel />
    </a-tab-pane>

    <!-- ── 本地模型（provider / 端点 / 模型名 / key）──
         仅本地模式展示：云端模式的模型与密钥由服务端托管，用户没有可填的端点。 ── -->
    <a-tab-pane
      v-if="deviceStore.isDesktop && deviceStore.isLocal()"
      key="local-model"
      :tab="$t('settings.local_model')"
    >
      <LocalModelPanel />
    </a-tab-pane>
  </a-tabs>
</template>

<style scoped>
/* 每个设置主题块之间用分隔线隔开，块内标题统一 */
.setting-block { padding: 4px 0 20px; }
.setting-block + .setting-block { border-top: var(--hairline-width) solid var(--border-card, var(--border)); padding-top: 20px; }
.setting-title {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 14px; font-weight: var(--fw-semibold); margin-bottom: 12px;
}
.setting-hint { font-size: 12px; font-weight: var(--fw-regular); color: var(--text-tertiary); }

.setting-form { max-width: 360px; }

.appearance-row { display: flex; align-items: center; gap: 16px; padding: 6px 0; flex-wrap: wrap; }
.appearance-label { font-size: 13px; color: var(--text-secondary); min-width: 72px; }
.accent-picker { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.accent-swatch {
  width: 26px; height: 26px; border-radius: 50%; box-shadow: inset 0 0 0 2px transparent;
  cursor: pointer; padding: 0; transition: transform var(--dur-fast) ease, box-shadow var(--dur-fast) ease;
}
.accent-swatch:hover { transform: scale(1.12); }
.accent-swatch.active { box-shadow: inset 0 0 0 2px var(--text-primary); }
.accent-custom {
  position: relative; width: 30px; height: 30px; border-radius: 50%; overflow: hidden;
  border: 2px solid var(--border); display: inline-flex; align-items: center; justify-content: center;
}
.accent-custom input { position: absolute; inset: -6px; width: 42px; height: 42px; opacity: 0; cursor: pointer; }

.identity-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; padding: 10px 0; border-bottom: var(--hairline-width) solid var(--border-card, var(--border));
}
.identity-row:last-of-type { border-bottom: none; }
.identity-info { display: flex; align-items: center; gap: 8px; min-width: 0; }
.identity-name {
  font-weight: var(--fw-medium); color: var(--text-primary);
  font-variant-numeric: tabular-nums;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.identity-meta { font-size: 12px; color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-icon { color: var(--text-tertiary); flex-shrink: 0; }
.row-actions { display: flex; gap: 8px; flex-shrink: 0; }

.bind-section { margin-top: 16px; }
.bind-title { font-size: 13px; color: var(--text-tertiary); margin-bottom: 8px; }
.bind-buttons { display: flex; flex-wrap: wrap; gap: 8px; }

.data-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.conv-list { max-height: 320px; overflow-y: auto; }
.conv-row {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 4px; cursor: pointer; border-radius: var(--radius-sm, 6px);
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.conv-row:hover { background: var(--bg-hover); }
.conv-title { flex: 1; min-width: 0; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv-meta { font-size: 12px; color: var(--text-tertiary); flex-shrink: 0; }

.legal-body { font-size: 13px; line-height: 1.7; color: var(--text-secondary); }
.legal-body p { margin: 0 0 10px; }

/* 窄屏：列表竖排、操作用整行，避免按钮挤压标题 */
@media (max-width: 576px) {
  .setting-form { max-width: 100%; }
  .identity-row { flex-direction: column; align-items: flex-start; gap: 6px; }
  .identity-info { width: 100%; }
  .row-actions { width: 100%; }
  .conv-meta { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .accent-swatch { transition: none; }
}
</style>
