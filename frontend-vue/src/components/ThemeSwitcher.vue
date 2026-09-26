<script setup lang="ts">
import { computed, ref } from 'vue'
import { Popover } from 'ant-design-vue'
import { BulbOutlined, BulbFilled, CheckOutlined, GlobalOutlined, HighlightOutlined, HighlightFilled } from '@ant-design/icons-vue'
import { useThemeStore } from '../stores/theme'
import type { ThemeId } from '../stores/theme'

const themeStore = useThemeStore()
const open = ref(false)

const presets = computed(() => themeStore.getAvailableThemes())

function selectTheme(id: ThemeId) {
  themeStore.setTheme(id)
}

function setMode(mode: 'light' | 'dark' | 'system') {
  themeStore.setMode(mode)
}

const currentTheme = computed(() => themeStore.activeThemeId)
const currentMode = computed(() => themeStore.resolvedMode)
const isSystem = computed(() => themeStore.preference === 'system')
</script>

<template>
  <Popover
    v-model:open="open"
    placement="bottomRight"
    trigger="click"
    :overlay-style="{ padding: '12px', minWidth: '260px' }"
    overlay-class-name="theme-switcher-popover"
  >
    <template #content>
      <div class="theme-switcher">
        <div class="ts-section">
          <div class="ts-section-title">
            {{ $t('settings.theme_style') }}
          </div>
          <div class="ts-grid">
            <button
              v-for="p in presets"
              :key="p.id"
              type="button"
              class="ts-theme"
              :class="{ active: currentTheme === p.id }"
              :title="p.description"
              @click="selectTheme(p.id)"
            >
              <span
                class="ts-swatch"
                :class="`ts-swatch--${p.id}`"
              >
                <span class="ts-swatch-dot ts-swatch-primary" />
                <span class="ts-swatch-dot ts-swatch-accent" />
                <span class="ts-swatch-dot ts-swatch-bg" />
              </span>
              <span class="ts-theme-info">
                <span class="ts-theme-name">{{ p.name }}</span>
                <span class="ts-theme-desc">{{ p.description }}</span>
              </span>
              <CheckOutlined
                v-if="currentTheme === p.id"
                class="ts-check"
              />
            </button>
          </div>
        </div>

        <div class="ts-section">
          <div class="ts-section-title">
            {{ $t('settings.appearance_mode') }}
          </div>
          <div class="ts-modes">
            <button
              type="button"
              class="ts-mode"
              :class="{ active: !isSystem && currentMode === 'light' }"
              @click="setMode('light')"
            >
              <HighlightOutlined /> {{ $t('common.light') }}
            </button>
            <button
              type="button"
              class="ts-mode"
              :class="{ active: !isSystem && currentMode === 'dark' }"
              @click="setMode('dark')"
            >
              <HighlightFilled /> {{ $t('common.dark') }}
            </button>
            <button
              type="button"
              class="ts-mode"
              :class="{ active: isSystem }"
              @click="setMode('system')"
            >
              <GlobalOutlined /> {{ $t('common.follow_system') }}
            </button>
          </div>
        </div>
      </div>
    </template>

    <button
      type="button"
      class="theme-switcher-btn"
      :title="$t('settings.theme_settings')"
      :aria-label="$t('settings.theme_settings')"
    >
      <BulbFilled v-if="themeStore.isDark" />
      <BulbOutlined v-else />
    </button>
  </Popover>
</template>

<style scoped>
.theme-switcher-btn {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all var(--dur-fast) ease;
  font-size: 16px;
}
.theme-switcher-btn:hover {
  color: var(--primary);
  border-color: var(--primary);
  background: var(--primary-bg);
}

.theme-switcher {
  color: var(--text-primary);
}
.ts-section {
  margin-bottom: 12px;
}
.ts-section:last-child {
  margin-bottom: 0;
}
.ts-section-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-tertiary);
  margin-bottom: 8px;
  padding: 0 2px;
}

.ts-grid {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ts-theme {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border-default);
  border-radius: 10px;
  background: var(--bg-surface);
  color: var(--text-primary);
  cursor: pointer;
  text-align: left;
  transition: all var(--dur-fast) ease;
}
.ts-theme:hover {
  border-color: var(--primary);
  background: var(--primary-bg);
}
.ts-theme.active {
  border-color: var(--primary);
  background: var(--primary-bg);
  box-shadow: 0 0 0 1px var(--primary-border);
}
.ts-swatch {
  display: inline-flex;
  gap: 3px;
  padding: 3px;
  border-radius: 8px;
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.ts-swatch-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: block;
}
.ts-swatch-primary {
  background: var(--primary);
}
.ts-swatch-accent {
  background: var(--accent);
}
.ts-swatch-bg {
  background: var(--bg-page);
  border: 1px solid var(--border-subtle);
}

.ts-theme-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.ts-theme-name {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.2;
}
.ts-theme-desc {
  font-size: 11px;
  color: var(--text-tertiary);
  line-height: 1.2;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ts-check {
  color: var(--primary);
  font-size: 12px;
  flex-shrink: 0;
}

.ts-modes {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 4px;
}
.ts-mode {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 7px 8px;
  font-size: 12px;
  font-weight: 500;
  border: 1px solid var(--border-default);
  border-radius: 8px;
  background: var(--bg-surface);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all var(--dur-fast) ease;
}
.ts-mode:hover {
  color: var(--primary);
  border-color: var(--primary);
}
.ts-mode.active {
  color: var(--primary);
  border-color: var(--primary);
  background: var(--primary-bg);
  font-weight: 600;
}
</style>

<style>
.theme-switcher-popover {
  padding: 12px !important;
  border-radius: 14px !important;
  border: 1px solid var(--border-default) !important;
  box-shadow: var(--shadow-lg) !important;
  background: var(--bg-elevated) !important;
}
.theme-switcher-popover .ant-popover-inner-content {
  padding: 0 !important;
}
</style>
