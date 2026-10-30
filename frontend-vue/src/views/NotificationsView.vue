<script setup lang="ts">
/**
 * 站内通知中心（P2-3）。
 *
 * 三条设计约束决定了这个页面的形态：
 *
 * 1. **站内通知不可关闭**。这里只有"是否同时发邮件"的开关，文案必须说"邮件"，
 *    说成"关闭通知"会让用户以为站内记录也会消失。
 * 2. **已读是弱状态**。列表默认展示全部（含已读），因为通知中心也是"最近发生了什么"
 *    的记录；只给未读会让用户第二天找不到昨天那笔支付通知。
 * 3. **点击即已读 + 跳转**。通知带 `link` 时点进去要落到对应页面，不然用户还得
 *    自己找"套餐在哪"。
 */
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Button, Segmented, Switch, Tag, message, Spin, Alert } from 'ant-design-vue'
import { BellOutlined, CheckOutlined, MailOutlined } from '@ant-design/icons-vue'
import dayjs from 'dayjs'
import EmptyState from '../components/common/EmptyState.vue'
import {
  listNotifications,
  markNotificationsRead,
  markAllNotificationsRead,
  getNotificationPrefs,
  updateNotificationPref,
  type NotificationItem,
} from '../api/notifications'
import { useI18n } from 'vue-i18n'
import { errorMessageText } from '../utils/apiError'
import {
  renderNotificationBody,
  renderNotificationTitle,
} from '../utils/notificationRender'
import type { Translate } from '../utils/notificationRender'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()

const items = ref<NotificationItem[]>([])
const unreadCount = ref(0)
const loading = ref(false)
const loadError = ref('')

/** 'all' | 'unread'。默认全部，理由见文件头取舍 2。 */
const filter = ref<'all' | 'unread'>('all')

// ── 邮件偏好 ──
const kinds = ref<string[]>([])
const emailEnabled = ref<Record<string, boolean>>({})
const prefsLoading = ref(false)
const prefsError = ref('')
const savingKind = ref('')
/** 偏好区根元素：供工具栏按钮与 hash 直达滚动过去。 */
const prefsSection = ref<HTMLElement | null>(null)

/**
 * 滚到邮件偏好区。
 *
 * 用 `scrollIntoView` 而不是 `router.push('#...')`：后者会新增一条历史记录，
 * 用户点完「返回」又回到列表顶部，但地址栏 hash 还在，再次进入会重复滚动 ——
 * 一个纯页内定位不该污染浏览历史。
 */
function scrollToPrefs(): void {
  prefsSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await listNotifications({
      limit: 100,
      unreadOnly: filter.value === 'unread',
    })
    items.value = res?.items ?? []
    unreadCount.value = res?.unread_count ?? 0
  } catch (e) {
    loadError.value = errorMessageText(e, t('notifications.load_failed'))
  } finally {
    loading.value = false
  }
}

async function loadPrefs() {
  prefsLoading.value = true
  prefsError.value = ''
  try {
    const res = await getNotificationPrefs()
    kinds.value = res?.kinds ?? []
    emailEnabled.value = res?.email_enabled ?? {}
  } catch (e) {
    prefsError.value = errorMessageText(e, t('notifications.prefs_load_failed'))
  } finally {
    prefsLoading.value = false
  }
}

onMounted(async () => {
  load()
  loadPrefs()

  // 外部链接 `/notifications#notification-prefs` 直达偏好区。
  // 等一个 tick 让偏好区渲染出来（它在列表之后，首屏可能还没挂载）。
  if (route.hash === '#notification-prefs') {
    await nextTick()
    scrollToPrefs()
  }
})

// 切换过滤条件要重新拉列表：`unread_only` 是后端参数，不是前端过滤 ——
// 前端过滤会让"未读数"和"列表长度"出现两个不一致的真相。
watch(filter, () => load())

const isEmpty = computed(() => !loading.value && !loadError.value && items.value.length === 0)

/** 事件类型标签。未知类型原样显示 kind，不隐藏 —— 隐藏会让用户以为没有这条通知。 */
function kindLabel(kind: string): string {
  const key = `notifications.kind.${kind}`
  const label = t(key)
  return label === key ? kind : label
}

/**
 * 按当前界面语言渲染通知标题。
 *
 * 实现见 `utils/notificationRender.ts`（纯函数，可单测四类事件 × 三语）。
 * 服务端只存 kind + payload（0011 取舍 6），于是**历史通知也会跟随语言切换**；
 * 任何一步拿不到参数就回落服务端给的 `item.title`。
 */
function renderTitle(item: NotificationItem): string {
  return renderNotificationTitle(item, t as Translate)
}

/** 按当前界面语言渲染通知正文；实现同上，参数不全时回落 `item.body`。 */
function renderBody(item: NotificationItem): string {
  return renderNotificationBody(item, t as Translate, formatDateOnly)
}

/** 正文里的日期只要日期部分（YYYY-MM-DD）；解析失败原样返回，不显示空。 */
function formatDateOnly(iso: string): string {
  const d = dayjs(iso)
  return d.isValid() ? d.format('YYYY-MM-DD') : ''
}

function formatTime(iso: string): string {
  if (!iso) return ''
  const d = dayjs(iso)
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm') : ''
}

async function markAll() {
  try {
    const marked = await markAllNotificationsRead()
    if (marked === 0) {
      message.info(t('notifications.nothing_to_mark'))
      return
    }
    message.success(t('notifications.marked_read', { n: marked }))
    unreadCount.value = 0
    // 通知顶栏角标立即刷新，不必等它自己的 5 分钟轮询。
    window.dispatchEvent(new Event('notification:read'))
    await load()
  } catch (e) {
    message.error(errorMessageText(e, t('notifications.load_failed')))
  }
}

/** 点击一条通知：先标已读，再跳到 link 指向的页面（没有 link 就只标已读）。 */
async function openItem(item: NotificationItem) {
  if (!item.read) {
    try {
      await markNotificationsRead([item.id])
      item.read = true
      item.read_at = new Date().toISOString()
      unreadCount.value = Math.max(0, unreadCount.value - 1)
      window.dispatchEvent(new Event('notification:read'))
      // 在"仅未读"视图里，标读后它应立刻消失 —— 否则用户会看到条目不走的怪现象。
      if (filter.value === 'unread') {
        items.value = items.value.filter((i) => i.id !== item.id)
      }
    } catch (e) {
      // 标记失败不阻止跳转：用户点的是"看详情"，不该被一次写失败挡住。
      message.error(errorMessageText(e, t('notifications.load_failed')))
    }
  }
  if (item.link) {
    router.push(item.link)
  }
}

async function toggleEmail(kind: string, next: boolean) {
  savingKind.value = kind
  // 乐观更新：开关必须立刻响应，否则用户会以为没点上而再点一次。
  const prev = emailEnabled.value[kind]
  emailEnabled.value = { ...emailEnabled.value, [kind]: next }
  try {
    const res = await updateNotificationPref(kind, next)
    if (res?.email_enabled) emailEnabled.value = res.email_enabled
    message.success(t('notifications.prefs_saved'))
  } catch (e) {
    // 失败回滚：宁可开关跳回去，也不要让界面显示一个没保存上的值。
    emailEnabled.value = { ...emailEnabled.value, [kind]: prev }
    message.error(errorMessageText(e, t('notifications.prefs_save_failed')))
  } finally {
    savingKind.value = ''
  }
}
</script>

<template>
  <div class="notifications-container">
    <header class="notifications-header">
      <h1>{{ $t('notifications.title') }}</h1>
      <Tag
        v-if="unreadCount > 0"
        color="red"
      >
        {{ unreadCount }} {{ $t('notifications.unread_badge') }}
      </Tag>
    </header>

    <div class="notifications-toolbar">
      <Segmented
        v-model:value="filter"
        :options="[
          { label: $t('notifications.filter_all'), value: 'all' },
          { label: $t('notifications.filter_unread'), value: 'unread' },
        ]"
        data-test="notifications-filter"
      />
      <div class="notifications-toolbar-actions">
        <!-- 偏好区在长列表下方，没有这个跳转用户得滚很久才找得到；
             而"关掉某类邮件"正是不堪其扰时的第一诉求。 -->
        <Button
          data-test="notifications-jump-prefs"
          @click="scrollToPrefs"
        >
          <template #icon>
            <MailOutlined />
          </template>
          {{ $t('notifications.prefs_jump') }}
        </Button>
        <Button
          :disabled="unreadCount === 0"
          data-test="notifications-mark-all"
          @click="markAll"
        >
          <template #icon>
            <CheckOutlined />
          </template>
          {{ $t('notifications.mark_all_read') }}
        </Button>
      </div>
    </div>

    <Alert
      v-if="loadError"
      type="error"
      :message="loadError"
      show-icon
      class="notifications-alert"
    >
      <template #action>
        <Button
          size="small"
          @click="load"
        >
          {{ $t('common.retry') }}
        </Button>
      </template>
    </Alert>

    <div
      v-if="loading && items.length === 0"
      class="notifications-loading"
    >
      <Spin />
    </div>

    <EmptyState
      v-else-if="isEmpty"
      :icon="BellOutlined"
      :description="filter === 'unread' ? $t('notifications.empty_unread') : $t('notifications.empty')"
      size="page"
    />

    <ul
      v-else
      class="notification-list"
    >
      <li
        v-for="item in items"
        :key="item.id"
        :class="{ unread: !item.read }"
        class="notification-item"
        :data-test="`notification-${item.id}`"
        @click="openItem(item)"
      >
        <div class="notification-main">
          <div class="notification-top">
            <Tag :color="item.read ? 'default' : 'blue'">
              {{ kindLabel(item.kind) }}
            </Tag>
            <span class="notification-time">{{ formatTime(item.created_at) }}</span>
          </div>
          <div class="notification-title">{{ renderTitle(item) }}</div>
          <div class="notification-body">{{ renderBody(item) }}</div>
        </div>
        <div
          v-if="item.link"
          class="notification-action"
        >
          {{ $t('notifications.view_detail') }}
        </div>
      </li>
    </ul>

    <!-- 邮件偏好：站内通知不可关闭，这里只控制是否同时发邮件。
         id 供两处使用：工具栏的「邮件设置」按钮跳转，以及外部链接
         `/notifications#notification-prefs` 直达（通知本身也常常指向这里）。 -->
    <section
      id="notification-prefs"
      ref="prefsSection"
      class="prefs-section"
    >
      <h2 class="prefs-title">
        <MailOutlined />
        {{ $t('notifications.prefs_title') }}
      </h2>
      <p class="prefs-note">
        {{ $t('notifications.prefs_note') }}
      </p>

      <Alert
        v-if="prefsError"
        type="warning"
        :message="prefsError"
        show-icon
        class="notifications-alert"
      />

      <div
        v-else-if="prefsLoading"
        class="notifications-loading"
      >
        <Spin size="small" />
      </div>

      <div
        v-else
        class="prefs-list"
      >
        <div
          v-for="kind in kinds"
          :key="kind"
          class="prefs-row"
        >
          <span class="prefs-kind">{{ kindLabel(kind) }}</span>
          <Switch
            :checked="emailEnabled[kind] !== false"
            :loading="savingKind === kind"
            :data-test="`notification-pref-${kind}`"
            @change="(v: unknown) => toggleEmail(kind, Boolean(v))"
          />
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.notifications-container {
  padding: 24px;
  max-width: 880px;
  margin: 0 auto;
}

.notifications-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}

.notifications-header h1 {
  margin: 0;
  font-size: 24px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}

.notifications-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.notifications-alert {
  margin-bottom: 16px;
}

/* 工具栏右侧的两个按钮：跳转偏好 + 全部标读。窄屏时随工具栏一起竖排。 */
.notifications-toolbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.notifications-loading {
  display: flex;
  justify-content: center;
  padding: 48px 0;
}

.notification-list {
  list-style: none;
  margin: 0 0 32px;
  padding: 0;
  border: 1px solid var(--border-color, rgba(0, 0, 0, 0.08));
  border-radius: 8px;
  overflow: hidden;
}

.notification-item {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 16px;
  cursor: pointer;
  border-block-end: 1px solid var(--border-color, rgba(0, 0, 0, 0.06));
  transition: background 0.15s ease;
}

.notification-item:last-child {
  border-block-end: none;
}

.notification-item:hover {
  background: var(--bg-secondary, rgba(0, 0, 0, 0.03));
}

/* 未读用左侧色条而不是加粗整段：加粗会让正文难以阅读，色条只标记"新" */
.notification-item.unread {
  box-shadow: inset 3px 0 0 var(--primary, #1677ff);
}

.notification-main {
  flex: 1;
  min-width: 0;
}

.notification-top {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.notification-time {
  font-size: 12px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.notification-title {
  font-size: 14px;
  font-weight: var(--fw-medium, 500);
  color: var(--text-primary);
  margin-bottom: 4px;
}

.notification-body {
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.notification-action {
  flex-shrink: 0;
  font-size: 13px;
  color: var(--primary, #1677ff);
}

.prefs-section {
  padding-block-start: 24px;
  border-block-start: 1px solid var(--border-color, rgba(0, 0, 0, 0.08));
}

.prefs-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 6px;
  font-size: 16px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}

.prefs-note {
  margin: 0 0 16px;
  font-size: 13px;
  color: var(--text-secondary);
}

.prefs-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.prefs-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
}

.prefs-kind {
  font-size: 14px;
  color: var(--text-primary);
}

@media (max-width: 640px) {
  .notifications-container { padding: 16px 12px; }
  .notifications-header h1 { font-size: 20px; }
  .notifications-toolbar { flex-direction: column; align-items: stretch; }
}
</style>
