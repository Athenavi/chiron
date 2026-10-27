<script setup lang="ts">
/**
 * 运行模式（local / cloud）区块 —— 设置页「运行模式」标签页的内容。
 *
 * 契约（docs/desktop-local/contracts.md §1）：
 *   · 切换会清掉**离开模式**的 cookie / localStorage / IndexedDB / CacheStorage /
 *     Service Worker / HTTP 缓存 / token，但**默认不删** `userData/local/**` 用户数据；
 *   · 那一个"是否同时删除本地数据"的选择必须由用户**显式勾选**（不可逆，见 §8）；
 *   · 切换后必须重启应用，因此这里不做"切换成功即生效"的乐观提示。
 *
 * 渲染进程**不自己删任何东西**：所有清理与写入都交给主进程（`setMode`），
 * 这里只负责把用户的选择与确认词收集齐。
 */
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { useDeviceStore, type ChironMode } from '../../stores/device'

const { t } = useI18n()
const device = useDeviceStore()

/** 确认词：切换代价不小（清数据 + 重启），要求手打而不是点两下 */
const CONFIRM_WORD = 'SWITCH'

const modalOpen = ref(false)
/** 目标模式（弹窗里选定的那个，不是当前模式） */
const target = ref<ChironMode>('cloud')
const confirmText = ref('')
const deleteLocalData = ref(false)
const switching = ref(false)

const currentLabel = computed(() =>
  device.isLocal() ? t('settings.run_mode_local') : t('settings.run_mode_cloud'))
const targetLabel = computed(() =>
  target.value === 'local' ? t('settings.run_mode_local') : t('settings.run_mode_cloud'))
const targetDesc = computed(() =>
  target.value === 'local' ? t('settings.run_mode_local_desc') : t('settings.run_mode_cloud_desc'))
const confirmOk = computed(() => confirmText.value.trim() === CONFIRM_WORD)

function openSwitch() {
  // 每次打开都重置：上一次的输入（尤其"删除本地数据"）不能残留成默认值
  target.value = device.isLocal() ? 'cloud' : 'local'
  confirmText.value = ''
  deleteLocalData.value = false
  modalOpen.value = true
}

async function submit() {
  if (!confirmOk.value || switching.value) return
  switching.value = true
  try {
    await device.setMode(target.value, { deleteLocalData: deleteLocalData.value })
    modalOpen.value = false
    // 先提示再重启：主进程若在 setMode 内部已 relaunch，这条提示可能一闪而过，
    // 但反过来（先重启再提示）则必然提示不到。
    message.success(t('settings.run_mode_switch_done'))
    // 重启失败不算切换失败：模式已写进 config.json，用户手动重开即可
    try {
      await device.restart()
    } catch {
      /* 提示里已写明"如未自动重启，请手动重开" */
    }
  } catch {
    message.error(t('settings.run_mode_switch_failed'))
  } finally {
    // 无论成功/失败都要复位：失败时用户会重试，卡在 loading 就再也点不动
    switching.value = false
  }
}

onMounted(() => {
  void device.ensureReady()
})
</script>

<template>
  <div class="run-mode-panel">
    <a-alert
      type="warning"
      show-icon
      class="mode-alert"
      :message="$t('settings.run_mode_warning_title')"
      :description="$t('settings.run_mode_warning_body')"
    />

    <div class="mode-block">
      <div class="mode-block-title">
        {{ $t('settings.run_mode') }}
      </div>
      <div class="mode-row">
        <span class="mode-label">{{ $t('settings.run_mode_current', { mode: currentLabel }) }}</span>
        <a-tag :color="device.isLocal() ? 'green' : 'blue'">
          {{ currentLabel }}
        </a-tag>
        <span class="mode-hint">{{ $t('settings.run_mode_hint') }}</span>
      </div>
      <div class="mode-row">
        <a-button
          type="primary"
          :disabled="!device.isDesktop"
          @click="openSwitch"
        >
          {{ $t('settings.run_mode_switch') }}
        </a-button>
        <span v-if="!device.isDesktop" class="mode-hint">
          {{ $t('settings.run_mode_desktop_only') }}
        </span>
      </div>
    </div>

    <!-- 切换确认：清哪些、保留哪些、可勾选的删除项、手打确认词 -->
    <a-modal
      v-model:open="modalOpen"
      :title="$t('settings.run_mode_switch_title', { mode: targetLabel })"
      :ok-text="$t('settings.run_mode_switch_confirm')"
      :cancel-text="$t('common.cancel')"
      :confirm-loading="switching"
      :ok-button-props="{ disabled: !confirmOk }"
      @ok="submit"
    >
      <p class="mode-modal-line">
        {{ targetDesc }}
      </p>
      <p class="mode-modal-line">
        {{ $t('settings.run_mode_switch_will_clear') }}
      </p>
      <ul class="mode-clear-list">
        <li>{{ $t('settings.run_mode_switch_clear_site_data') }}</li>
        <li>{{ $t('settings.run_mode_switch_clear_cache') }}</li>
        <li>{{ $t('settings.run_mode_switch_clear_token') }}</li>
      </ul>
      <p class="mode-modal-line">
        {{ $t('settings.run_mode_keep_local_data') }}
      </p>
      <!-- 只有离开本地模式时"本地数据"才会被遗留在磁盘上，故仅此时给出勾选项 -->
      <a-checkbox v-if="target === 'cloud'" v-model:checked="deleteLocalData">
        {{ $t('settings.run_mode_delete_local_data') }}
      </a-checkbox>
      <div class="mode-confirm">
        <div class="mode-confirm-label">
          {{ $t('settings.run_mode_confirm_hint', { word: CONFIRM_WORD }) }}
        </div>
        <a-input
          v-model:value="confirmText"
          :placeholder="$t('settings.run_mode_confirm_placeholder', { word: CONFIRM_WORD })"
        />
      </div>
    </a-modal>
  </div>
</template>

<style scoped>
.mode-alert { margin-bottom: 16px; }
.mode-block { padding-block: 4px 20px; }
.mode-block-title { font-weight: var(--fw-semibold); color: var(--text-primary); margin-bottom: 12px; }
.mode-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.mode-label { color: var(--text-primary); }
.mode-hint { color: var(--text-secondary); font-size: 12px; }
.mode-modal-line { margin: 0 0 10px; color: var(--text-primary); }
.mode-clear-list {
  margin: 0 0 12px;
  padding-inline-start: 20px;
  color: var(--text-secondary);
  font-size: 13px;
}
.mode-confirm { margin-top: 16px; padding-top: 12px; border-top: var(--hairline-width) solid var(--border-card, var(--border)); }
.mode-confirm-label { margin-bottom: 8px; color: var(--text-secondary); font-size: 13px; }
</style>
