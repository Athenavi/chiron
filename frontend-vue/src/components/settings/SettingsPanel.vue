<script setup lang="ts">
/**
 * 用户设置面板：通用 / 账户 / 数据 / 协议四个标签页。
 *
 * 页面（/profile）与头像弹窗共用本组件 —— 两套入口一套实现，避免各自漂移。
 * 组件本身不含容器（无 header、无尺寸），由调用方决定是页面还是弹窗。
 */
import { ref, onMounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import { Button, Input, Form, FormItem, message, Popconfirm, Tag, Spin, Slider } from 'ant-design-vue'
import { SafetyOutlined, MobileOutlined, ExportOutlined, DeleteOutlined, LinkOutlined } from '@ant-design/icons-vue'
import EmptyState from '../common/EmptyState.vue'
import { useAuthStore } from '../../stores/auth'
import { useThemeStore } from '../../stores/theme'
import { api } from '../../api'
import {
  listIdentities,
  deleteIdentity,
  setPassword,
  listPublicSsoProviders,
  ssoLoginURL,
  getSmsBind,
  bindPhone,
  unbindPhone,
  sendSmsCode,
  isValidPhone,
  type UserIdentity,
  type SsoPublicProvider,
} from '../../api/auth'
import { useSmsCountdown } from '../../composables/useSmsCountdown'
import { useSpeech } from '../../composables/useSpeech'
import { ACCENT_PRESETS, DEFAULT_ACCENT } from '../../utils/accentPresets'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const props = withDefaults(defineProps<{ initialTab?: string }>(), { initialTab: 'general' })

const route = useRoute()
const authStore = useAuthStore()
const themeStore = useThemeStore()

const activeTab = ref(props.initialTab)

// ── 账户资料 ──

const loading = ref(false)
const form = ref({ name: '', email: '' })

async function handleUpdateProfile() {
  loading.value = true
  try {
    await api.put('/v1/auth/profile', form.value)
    message.success(t('个人信息已更新'))
    await authStore.fetchProfile()
  } catch (error: any) {
    message.error(error.message || t('更新失败'))
  } finally {
    loading.value = false
  }
}

// ── 三方账号绑定 ──

const identities = ref<UserIdentity[]>([])
const bindable = ref<SsoPublicProvider[]>([])
const bindingsLoading = ref(false)
const unbinding = ref('')

async function loadBindings() {
  bindingsLoading.value = true
  try {
    const [ids, providers] = await Promise.all([
      listIdentities(),
      listPublicSsoProviders().catch(() => [] as SsoPublicProvider[]),
    ])
    identities.value = ids
    // 已绑定的 provider 不再出现在可绑定列表
    const boundNames = new Set(ids.map(i => i.provider_name))
    bindable.value = providers.filter(p => !boundNames.has(p.display_name) && !boundNames.has(p.name))
  } catch (e: any) {
    message.error(e.response?.data?.error || t('绑定信息加载失败'))
  } finally {
    bindingsLoading.value = false
  }
}

function startBind(providerId: string) {
  // 整页跳转：网关域 cookie 随导航携带，bind state 由后端签发
  window.location.href = ssoLoginURL(providerId, 'bind')
}

async function handleUnbind(id: string) {
  unbinding.value = id
  try {
    await deleteIdentity(id)
    message.success(t('已解绑'))
    await loadBindings()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('解绑失败'))
  } finally {
    unbinding.value = ''
  }
}

// ── 手机号绑定（短信验证码）──

const phone = ref('')
const phoneBound = ref(false)
const phoneLoading = ref(false)
const phoneForm = ref({ phone: '', code: '' })
const phoneBinding = ref(false)
const unbindingPhone = ref(false)
const { remaining: phoneCountdown, start: startPhoneCountdown } = useSmsCountdown(60)
const sendingCode = ref(false)

async function loadPhone() {
  phoneLoading.value = true
  try {
    const res = await getSmsBind()
    phone.value = res.phone || ''
    phoneBound.value = !!res.bound
  } catch {
    // 短信服务未配置/不可达时静默隐藏绑定表单
    phoneBound.value = false
  } finally {
    phoneLoading.value = false
  }
}

async function handleSendBindCode() {
  const p = phoneForm.value.phone.trim()
  if (!isValidPhone(p)) {
    message.warning(t('请输入正确的手机号'))
    return
  }
  sendingCode.value = true
  try {
    const res = await sendSmsCode({ phone: p, purpose: 'bind' })
    startPhoneCountdown(res.interval || 60)
    message.success(t('验证码已发送'))
  } catch (e: any) {
    const status = e.response?.status
    const apiErr = e.response?.data?.error
    if (status === 429) {
      message.error(t('发送过于频繁，请稍后再试'))
    } else if (status === 403) {
      message.error(apiErr || t('短信服务未启用'))
    } else {
      message.error(apiErr || t('验证码发送失败'))
    }
  } finally {
    sendingCode.value = false
  }
}

async function handleBindPhone() {
  const { phone: p, code } = phoneForm.value
  if (!isValidPhone(p) || !code.trim()) {
    message.warning(t('请填写手机号与验证码'))
    return
  }
  phoneBinding.value = true
  try {
    await bindPhone({ phone: p.trim(), code: code.trim() })
    message.success(t('手机号绑定成功'))
    phoneForm.value = { phone: '', code: '' }
    await loadPhone()
  } catch (e: any) {
    const status = e.response?.status
    const apiErr = e.response?.data?.error
    if (status === 409) {
      message.error(t('该手机号已绑定其他账号'))
    } else {
      message.error(apiErr || t('绑定失败'))
    }
  } finally {
    phoneBinding.value = false
  }
}

async function handleUnbindPhone() {
  unbindingPhone.value = true
  try {
    await unbindPhone()
    message.success(t('已解绑手机号'))
    await loadPhone()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('解绑失败'))
  } finally {
    unbindingPhone.value = false
  }
}

// ── 设置密码（SSO 建号用户首设免旧密码）──

const pwdForm = ref({ current_password: '', new_password: '', confirm: '' })
const pwdLoading = ref(false)

async function handleSetPassword() {
  const { current_password, new_password, confirm } = pwdForm.value
  if (new_password.length < 8 || new_password.length > 128) {
    message.warning(t('新密码需 8-128 位'))
    return
  }
  if (new_password !== confirm) {
    message.warning(t('两次输入的新密码不一致'))
    return
  }
  pwdLoading.value = true
  try {
    await setPassword({ current_password: current_password || undefined, new_password })
    message.success(t('密码已设置'))
    pwdForm.value = { current_password: '', new_password: '', confirm: '' }
  } catch (e: any) {
    const apiErr = e.response?.data?.error
    if (apiErr === 'current_password is required') {
      message.error(t('该账号已设置密码，请先输入当前密码'))
    } else if (apiErr === 'invalid current password') {
      message.error(t('当前密码不正确'))
    } else {
      message.error(apiErr || t('设置失败'))
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
    message.success(t('朗读设置已保存'))
    await authStore.fetchProfile()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('保存失败'))
  } finally {
    speechSaving.value = false
  }
}

function previewSpeech() {
  if (!speak(t('这是一段朗读试听。'), speech.value)) {
    message.warning(t('请先选择音色，或当前浏览器不支持朗读'))
  }
}

// ── 数据管理：分享 ──

interface ShareItem {
  share_id: string
  session_id: string
  title: string
  message_count: number
  created_at: string
}

const shares = ref<ShareItem[]>([])
const sharesLoading = ref(false)
const revoking = ref('')

async function loadShares() {
  sharesLoading.value = true
  try {
    const { data } = await api.get('/v1/shares')
    shares.value = data?.data?.items ?? []
  } catch (e: any) {
    message.error(e.response?.data?.error || t('分享列表加载失败'))
  } finally {
    sharesLoading.value = false
  }
}

async function handleRevokeShare(item: ShareItem) {
  revoking.value = item.share_id
  try {
    // 撤销是按会话维度的（后端会把该会话所有有效分享标记 revoked）
    await api.delete(`/v1/conversations/${item.session_id}/share`)
    message.success(t('已取消分享'))
    await loadShares()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('取消分享失败'))
  } finally {
    revoking.value = ''
  }
}

function shareURL(id: string) {
  return `${window.location.origin}/share/${id}`
}

async function copyShareURL(id: string) {
  try {
    await navigator.clipboard.writeText(shareURL(id))
    message.success(t('分享链接已复制'))
  } catch {
    message.warning(t('复制失败，请手动复制'))
  }
}

// ── 数据管理：会话导出与删除 ──

interface ConversationItem {
  id: string
  title?: string
  updated_at?: string
  created_at?: string
}

const conversations = ref<ConversationItem[]>([])
const convLoading = ref(false)
const selectedConvIds = ref<string[]>([])
const exporting = ref(false)
const deleting = ref(false)

async function loadConversations() {
  convLoading.value = true
  try {
    const { data } = await api.get('/v1/conversations')
    // 兼容 {items:[...]} 与直接数组两种返回形态
    conversations.value = data?.data?.items ?? data?.data ?? []
  } catch (e: any) {
    message.error(e.response?.data?.error || t('会话列表加载失败'))
  } finally {
    convLoading.value = false
  }
}

/** 把一次会话渲染成 Markdown；reasoning 单独成节，避免与正文混淆 */
function conversationToMarkdown(conv: ConversationItem, messages: any[]): string {
  const lines: string[] = [
    t('# {title}', { title: conv.title || t('未命名会话') }),
    '',
    t('> 导出时间：{time}', { time: new Date().toLocaleString() }),
    '',
  ]
  for (const m of messages) {
    const role = m.role === 'user' ? t('用户') : m.role === 'assistant' ? t('助手') : (m.role || t('未知'))
    lines.push(`## ${role}`, '')
    if (m.reasoning) {
      lines.push('<details><summary>' + t('思考过程') + '</summary>', '', String(m.reasoning), '', '</details>', '')
    }
    lines.push(String(m.content ?? ''), '')
  }
  return lines.join('\n')
}

/** 逐会话拉详情再打包。导出是低频操作，串行可避免瞬时打满后端。 */
async function exportSelected() {
  const ids = selectedConvIds.value
  if (!ids.length) {
    message.warning(t('请先选择要导出的会话'))
    return
  }
  exporting.value = true
  try {
    const parts: string[] = []
    for (const id of ids) {
      const conv = conversations.value.find(c => c.id === id) ?? { id }
      const { data } = await api.get(`/v1/conversations/${id}`)
      const payload = data?.data ?? {}
      const messages = payload.messages ?? payload.items ?? []
      parts.push(conversationToMarkdown(conv, messages), '\n---\n')
    }
    const blob = new Blob([parts.join('\n')], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `chiron-conversations-${new Date().toISOString().slice(0, 10)}.md`
    a.click()
    URL.revokeObjectURL(url)
    message.success(t('已导出 {n} 个会话', { n: ids.length }))
  } catch (e: any) {
    message.error(e.response?.data?.error || t('导出失败'))
  } finally {
    exporting.value = false
  }
}

async function deleteSelected() {
  const ids = selectedConvIds.value
  if (!ids.length) {
    message.warning(t('请先选择要删除的会话'))
    return
  }
  deleting.value = true
  try {
    // 后端无批量接口，逐个删除；部分失败时如实报告成功数，不谎报全部成功
    let ok = 0
    const failed: string[] = []
    for (const id of ids) {
      try {
        await api.delete(`/v1/conversations/${id}`)
        ok++
      } catch {
        failed.push(id)
      }
    }
    if (failed.length) {
      message.warning(t('已删除 {ok} 个，失败 {failed} 个', { ok, failed: failed.length }))
    } else {
      message.success(t('已删除 {n} 个会话', { n: ok }))
    }
    selectedConvIds.value = failed
    await loadConversations()
    await loadShares()
  } finally {
    deleting.value = false
  }
}

const allSelected = computed(() =>
  conversations.value.length > 0 && selectedConvIds.value.length === conversations.value.length)

function toggleSelectAll() {
  selectedConvIds.value = allSelected.value ? [] : conversations.value.map(c => c.id)
}

const selectedCount = computed(() => selectedConvIds.value.length)

// ── 生命周期 ──

onMounted(async () => {
  if (authStore.user) {
    form.value.name = authStore.user.name || ''
    form.value.email = authStore.user.email || ''
  }
  // 绑定成功回跳（bindURL=/profile?bind=ok）；弹窗入口不会带该参数
  if (route.query.bind === 'ok') {
    message.success(t('三方账号绑定成功'))
  }
  loadSpeechPrefs()
  loadBindings()
  loadPhone()
  loadShares()
  loadConversations()
})
</script>

<template>
  <a-tabs v-model:active-key="activeTab">
    <!-- ── 通用设置 ── -->
    <a-tab-pane
      key="general"
      :tab="$t('通用设置')"
    >
      <div class="setting-block">
        <div class="setting-title">
          {{ $t('外观与主题') }}
        </div>
        <div class="appearance-row">
          <div class="appearance-label">
            {{ $t('深浅模式') }}
          </div>
          <a-radio-group
            :value="themeStore.preference"
            @change="() => themeStore.toggleTheme()"
          >
            <a-radio-button :value="'dark'">
              {{ $t('深色') }}
            </a-radio-button>
            <a-radio-button :value="'light'">
              {{ $t('浅色') }}
            </a-radio-button>
          </a-radio-group>
        </div>
        <div class="appearance-row">
          <div class="appearance-label">
            {{ $t('强调色') }}
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
              :title="$t('自定义颜色')"
            >
              <input
                type="color"
                :value="themeStore.accent"
                @input="(e: any) => themeStore.setAccent(e.target.value)"
              >
            </label>
          </div>
        </div>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('朗读') }}
          <span class="setting-hint">{{ $t('使用系统语音，无需联网') }}</span>
        </div>
        <EmptyState
          v-if="!ttsSupported"
          :description="$t('当前浏览器不支持语音朗读')"
        />
        <template v-else>
          <div class="appearance-row">
            <div class="appearance-label">
              {{ $t('音色') }}
            </div>
            <a-select
              v-model:value="speech.voiceURI"
              style="min-width: 240px; flex: 1"
              show-search
              option-filter-prop="label"
              :placeholder="$t('系统默认')"
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
                  :label="v.localService ? v.name : $t('{name}（在线）', { name: v.name })"
                >
                  {{ v.name }}<span v-if="!v.localService">{{ $t('（在线）') }}</span>
                </a-select-option>
              </a-select-opt-group>
            </a-select>
          </div>
          <div class="appearance-row">
            <div class="appearance-label">
              {{ $t('语速') }}
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
              {{ $t('音调') }}
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
              {{ $t('试听') }}
            </Button>
            <Button
              v-if="speaking"
              danger
              @click="stop"
            >
              {{ $t('停止') }}
            </Button>
            <Button
              type="primary"
              :loading="speechSaving"
              @click="saveSpeechPrefs"
            >
              {{ $t('保存设置') }}
            </Button>
          </div>
        </template>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('语言') }}
          <span class="setting-hint">{{ $t('即将支持') }}</span>
        </div>
        <EmptyState :description="$t('多语言界面尚在开发中，目前为简体中文')" />
      </div>
    </a-tab-pane>

    <!-- ── 账户设置 ── -->
    <a-tab-pane
      key="account"
      :tab="$t('账户设置')"
    >
      <div class="setting-block">
        <div class="setting-title">
          {{ $t('基本资料') }}
        </div>
        <Form
          :model="form"
          layout="vertical"
          class="setting-form"
        >
          <FormItem :label="$t('用户名')">
            <Input
              v-model:value="form.name"
              :placeholder="$t('请输入用户名')"
            />
          </FormItem>
          <FormItem :label="$t('邮箱')">
            <Input
              v-model:value="form.email"
              :placeholder="$t('请输入邮箱')"
              disabled
            />
          </FormItem>
          <FormItem>
            <Button
              type="primary"
              :loading="loading"
              @click="handleUpdateProfile"
            >
              {{ $t('保存修改') }}
            </Button>
          </FormItem>
        </Form>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('三方账号绑定') }}
        </div>
        <Spin :spinning="bindingsLoading">
          <EmptyState
            v-if="!identities.length && !bindable.length"
            :description="$t('暂无可用的三方登录方式')"
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
                :title="$t('确定解绑该三方账号？')"
                :ok-text="$t('解绑')"
                :cancel-text="$t('取消')"
                @confirm="handleUnbind(item.id)"
              >
                <Button
                  danger
                  :loading="unbinding === item.id"
                >
                  {{ $t('解绑') }}
                </Button>
              </Popconfirm>
            </div>
            <div
              v-if="bindable.length"
              class="bind-section"
            >
              <div class="bind-title">
                {{ $t('可绑定的三方账号') }}
              </div>
              <div class="bind-buttons">
                <Button
                  v-for="p in bindable"
                  :key="p.id"
                  @click="startBind(p.id)"
                >
                  {{ $t('绑定 {name}', { name: p.display_name || p.name }) }}
                </Button>
              </div>
            </div>
          </template>
        </Spin>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('手机号') }}
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
              <span class="identity-meta">{{ $t('可用于短信验证码登录') }}</span>
            </div>
            <Popconfirm
              :title="$t('确定解绑该手机号？')"
              :ok-text="$t('解绑')"
              :cancel-text="$t('取消')"
              @confirm="handleUnbindPhone"
            >
              <Button
                danger
                :loading="unbindingPhone"
              >
                {{ $t('解绑') }}
              </Button>
            </Popconfirm>
          </div>
          <Form
            v-else
            :model="phoneForm"
            layout="vertical"
            class="setting-form"
          >
            <FormItem :label="$t('手机号')">
              <Input
                v-model:value="phoneForm.phone"
                :placeholder="$t('请输入手机号')"
                :maxlength="21"
              >
                <template #prefix>
                  <MobileOutlined />
                </template>
              </Input>
            </FormItem>
            <FormItem :label="$t('验证码')">
              <Input
                v-model:value="phoneForm.code"
                :placeholder="$t('短信验证码')"
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
                    {{ phoneCountdown > 0 ? $t('{n}s 后重发', { n: phoneCountdown }) : $t('获取验证码') }}
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
                {{ $t('绑定手机号') }}
              </Button>
            </FormItem>
          </Form>
        </Spin>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('设置密码') }}
          <span class="setting-hint"><SafetyOutlined /> {{ $t('三方登录账号首次设置密码无需当前密码') }}</span>
        </div>
        <Form
          :model="pwdForm"
          layout="vertical"
          class="setting-form"
        >
          <FormItem :label="$t('当前密码（首次设置可留空）')">
            <Input
              v-model:value="pwdForm.current_password"
              type="password"
              :placeholder="$t('已设置过密码的账号必填')"
            />
          </FormItem>
          <FormItem :label="$t('新密码')">
            <Input
              v-model:value="pwdForm.new_password"
              type="password"
              :placeholder="$t('8-128 位')"
            />
          </FormItem>
          <FormItem :label="$t('确认新密码')">
            <Input
              v-model:value="pwdForm.confirm"
              type="password"
              :placeholder="$t('再次输入新密码')"
            />
          </FormItem>
          <FormItem>
            <Button
              type="primary"
              :loading="pwdLoading"
              @click="handleSetPassword"
            >
              {{ $t('保存密码') }}
            </Button>
          </FormItem>
        </Form>
      </div>
    </a-tab-pane>

    <!-- ── 数据管理 ── -->
    <a-tab-pane
      key="data"
      :tab="$t('数据管理')"
    >
      <div class="setting-block">
        <div class="setting-title">
          {{ $t('分享管理') }}
          <span class="setting-hint">{{ $t('已公开的对话；取消后链接立即失效') }}</span>
        </div>
        <Spin :spinning="sharesLoading">
          <EmptyState
            v-if="!shares.length"
            :description="$t('尚未分享任何对话')"
          />
          <div
            v-for="item in shares"
            :key="item.share_id"
            class="identity-row"
          >
            <div class="identity-info">
              <LinkOutlined class="row-icon" />
              <span class="identity-name">{{ item.title || $t('未命名对话') }}</span>
              <span class="identity-meta">
                {{ $t('{n} 条消息 · {time}', { n: item.message_count, time: new Date(item.created_at).toLocaleString() }) }}
              </span>
            </div>
            <div class="row-actions">
              <Button
                size="small"
                @click="copyShareURL(item.share_id)"
              >
                {{ $t('复制链接') }}
              </Button>
              <Popconfirm
                :title="$t('确定取消该分享？链接将立即失效。')"
                :ok-text="$t('取消分享')"
                :cancel-text="$t('返回')"
                @confirm="handleRevokeShare(item)"
              >
                <Button
                  size="small"
                  danger
                  :loading="revoking === item.share_id"
                >
                  {{ $t('取消分享') }}
                </Button>
              </Popconfirm>
            </div>
          </div>
        </Spin>
      </div>

      <div class="setting-block">
        <div class="setting-title">
          {{ $t('历史会话') }}
          <span class="setting-hint">
            {{ $t('已选 {sel} / {total}', { sel: selectedCount, total: conversations.length }) }}
          </span>
        </div>
        <div class="data-toolbar">
          <Button
            size="small"
            @click="toggleSelectAll"
          >
            {{ allSelected ? $t('取消全选') : $t('全选') }}
          </Button>
          <Button
            size="small"
            :disabled="!selectedCount"
            :loading="exporting"
            @click="exportSelected"
          >
            <ExportOutlined /> {{ $t('导出所选（Markdown）') }}
          </Button>
          <Popconfirm
            :title="$t('确定删除所选会话？此操作不可恢复。')"
            :ok-text="$t('删除')"
            :cancel-text="$t('取消')"
            @confirm="deleteSelected"
          >
            <Button
              size="small"
              danger
              :disabled="!selectedCount"
              :loading="deleting"
            >
              <DeleteOutlined /> {{ $t('删除所选') }}
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
              <span class="conv-title">{{ c.title || $t('未命名会话') }}</span>
              <span class="conv-meta">{{ c.updated_at ? new Date(c.updated_at).toLocaleString() : '' }}</span>
            </label>
            <EmptyState
              v-if="!conversations.length"
              :description="$t('暂无历史会话')"
            />
          </div>
        </Spin>
      </div>
    </a-tab-pane>

    <!-- ── 服务协议 ── -->
    <a-tab-pane
      key="legal"
      :tab="$t('服务协议')"
    >
      <div class="setting-block">
        <div class="setting-title">
          {{ $t('用户协议') }}
        </div>
        <div class="legal-body">
          <p>{{ $t('欢迎使用 Chiron。使用本服务前，请阅读并同意以下条款。') }}</p>
          <p><strong>{{ $t('1. 服务内容。') }}</strong>{{ $t('本服务提供基于大语言模型的对话、知识库与工作流编排能力。服务可能因维护、升级或不可抗力中断。') }}</p>
          <p><strong>{{ $t('2. 账号责任。') }}</strong>{{ $t('你需妥善保管账号凭据，并对账号下的全部活动负责。发现未经授权的使用应立即通知我们。') }}</p>
          <p><strong>{{ $t('3. 使用规范。') }}</strong>{{ $t('不得利用本服务生成、传播违法信息，不得实施攻击、爬取、逆向等危害服务与其他用户的行为。') }}</p>
          <p><strong>{{ $t('4. 内容归属。') }}</strong>{{ $t('你输入的内容归你所有；生成内容的使用需遵守适用法律。你需对自己发布的内容负责。') }}</p>
          <p><strong>{{ $t('5. 服务变更与终止。') }}</strong>{{ $t('我们可能调整或终止部分功能，重大变更会提前通知。') }}</p>
        </div>
      </div>
      <div class="setting-block">
        <div class="setting-title">
          {{ $t('隐私政策') }}
        </div>
        <div class="legal-body">
          <p><strong>{{ $t('1. 收集范围。') }}</strong>{{ $t('我们收集账号信息（邮箱、用户名）、你主动提交的对话与知识库内容，以及必要的访问日志。') }}</p>
          <p><strong>{{ $t('2. 使用目的。') }}</strong>{{ $t('用于提供服务、保障安全与改进产品，不用于与服务无关的用途。') }}</p>
          <p><strong>{{ $t('3. 存储与保护。') }}</strong>{{ $t('数据存储于你部署的实例；我们采取访问控制与传输加密等措施。') }}</p>
          <p><strong>{{ $t('4. 第三方共享。') }}</strong>{{ $t('除法律要求或你明确授权外，不向第三方提供你的个人数据。') }}</p>
          <p><strong>{{ $t('5. 你的权利。') }}</strong>{{ $t('你可在「数据管理」中导出或删除历史会话，也可在「账户设置」中解绑三方账号、手机号。') }}</p>
          <p><strong>{{ $t('6. 联系。') }}</strong>{{ $t('对本政策有疑问，请通过部署方提供的渠道联系我们。') }}</p>
        </div>
      </div>
    </a-tab-pane>
  </a-tabs>
</template>

<style scoped>
/* 每个设置主题块之间用分隔线隔开，块内标题统一 */
.setting-block { padding: 4px 0 20px; }
.setting-block + .setting-block { border-top: 1px solid var(--border-card, var(--border)); padding-top: 20px; }
.setting-title {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 14px; font-weight: 600; margin-bottom: 12px;
}
.setting-hint { font-size: 12px; font-weight: 400; color: var(--text-tertiary); }

.setting-form { max-width: 360px; }

.appearance-row { display: flex; align-items: center; gap: 16px; padding: 6px 0; flex-wrap: wrap; }
.appearance-label { font-size: 13px; color: var(--text-secondary); min-width: 72px; }
.accent-picker { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.accent-swatch {
  width: 26px; height: 26px; border-radius: 50%; border: 2px solid transparent;
  cursor: pointer; padding: 0; transition: transform var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.accent-swatch:hover { transform: scale(1.12); }
.accent-swatch.active { border-color: var(--text-primary); }
.accent-custom {
  position: relative; width: 30px; height: 30px; border-radius: 50%; overflow: hidden;
  border: 2px solid var(--border); display: inline-flex; align-items: center; justify-content: center;
}
.accent-custom input { position: absolute; inset: -6px; width: 42px; height: 42px; opacity: 0; cursor: pointer; }

.identity-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; padding: 10px 0; border-bottom: 1px solid var(--border-card, var(--border));
}
.identity-row:last-of-type { border-bottom: none; }
.identity-info { display: flex; align-items: center; gap: 8px; min-width: 0; }
.identity-name {
  font-weight: 500; color: var(--text-primary);
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
