<script setup lang="ts">
/**
 * 快捷键速查表（`?` 打开）。
 *
 * 与设置页共用同一份目录与同一份改键状态，所以这里显示的**就是**当前生效的键位 ——
 * 用户改过键之后，速查表不会还停留在默认值上。
 *
 * 只读：改键在设置页做。速查表是"我忘了键是什么"时按 `?` 看一眼的地方。
 */
import { computed } from 'vue'
import { Modal } from 'ant-design-vue'
import { comboLabel, useShortcutPreferences } from '../../composables/useKeyboardShortcuts'
import {
  SHORTCUT_CATALOG,
  SHORTCUT_GROUP_LABEL_KEY,
  type ShortcutGroup,
} from '../../config/shortcutCatalog'
import { useAuthStore } from '../../stores/auth'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const authStore = useAuthStore()
const prefs = useShortcutPreferences({
  specs: SHORTCUT_CATALOG,
  userId: authStore.user?.id ?? null,
})

const isMac = typeof navigator !== 'undefined'
  && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)

const GROUPS: readonly ShortcutGroup[] = ['navigation', 'conversation', 'panel']

const grouped = computed(() =>
  GROUPS.map(group => ({
    group,
    titleKey: SHORTCUT_GROUP_LABEL_KEY[group],
    items: SHORTCUT_CATALOG.filter(s => s.group === group),
  })).filter(g => g.items.length > 0))
</script>

<template>
  <Modal
    :open="open"
    :title="$t('settings.shortcuts_help_title')"
    :footer="null"
    width="520px"
    @cancel="emit('update:open', false)"
  >
    <div class="help-hint">
      {{ $t('settings.shortcuts_help_hint') }}
    </div>

    <div
      v-for="section in grouped"
      :key="section.group"
      class="help-group"
    >
      <div class="help-group-title">
        {{ $t(section.titleKey) }}
      </div>
      <div
        v-for="spec in section.items"
        :key="spec.id"
        class="help-row"
      >
        <span class="help-name">{{ $t(spec.labelKey) }}</span>
        <kbd class="help-key">{{ comboLabel(prefs.keysOf(spec.id), isMac) }}</kbd>
      </div>
    </div>
  </Modal>
</template>

<style scoped>
.help-hint {
  color: var(--text-secondary);
  font-size: 12px;
  margin-bottom: 16px;
}
.help-group { margin-bottom: 16px; }
.help-group-title {
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
  margin-bottom: 6px;
}
.help-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-block: 4px;
}
.help-name { color: var(--text-primary); font-size: 13px; }
.help-key {
  margin-inline-start: auto;
  font-family: var(--font-mono, 'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace);
  font-size: 12px;
  padding: 2px 8px;
  border: var(--hairline-width) solid var(--border-card, var(--border));
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-code, transparent);
  color: var(--text-primary);
  white-space: nowrap;
}
</style>
