<!-- SkillMarketCard - 市场卡片组件（技能 / Agent / MCP 三端通用） -->
<template>
  <div class="market-panel">
    <div class="market-toolbar">
      <Input
        v-model:value="searchQuery"
        :placeholder="searchPlaceholder"
        allow-clear
        class="market-search"
      >
        <template #prefix>
          <SearchOutlined />
        </template>
      </Input>
    </div>

    <EmptyState
      v-if="filteredItems.length === 0"
      size="page"
      :icon="markRaw(typeIcons[type])"
      :description="searchQuery ? $t('暂无匹配的市场条目') : $t('市场暂无内容')"
      :hint="searchQuery ? $t('尝试调整搜索关键词') : $t('管理员发布市场条目后，将展示在这里')"
    />

    <div
      v-else
      class="market-grid"
    >
      <div
        v-for="item in filteredItems"
        :key="item.id"
        class="market-card"
        :class="{ installed: item.installed }"
      >
        <div class="card-top">
          <span class="card-icon"><component :is="typeIcons[type]" /></span>
          <div class="card-titles">
            <span class="card-name">{{ displayName(item) }}</span>
            <span class="card-desc">{{ displayDesc(item) }}</span>
          </div>
          <Tag
            v-if="item.installed"
            color="green"
            class="installed-tag"
          >
            {{ $t('已安装') }}
          </Tag>
        </div>

        <!-- Agent：系统提示词摘要 -->
        <div
          v-if="type === 'agent' && systemPrompt(item)"
          class="prompt-preview"
        >
          <div class="prompt-label">
            {{ $t('系统提示词') }}
          </div>
          <div class="prompt-text">
            {{ systemPrompt(item) }}
          </div>
        </div>

        <!-- MCP：命令与参数 -->
        <div
          v-else-if="type === 'mcp'"
          class="mcp-info"
        >
          <div class="mcp-line">
            <span class="mcp-label">command</span>
            <code class="mcp-code">{{ mcpCommand(item) || '—' }}</code>
          </div>
          <div
            v-if="mcpArgs(item).length"
            class="mcp-line"
          >
            <span class="mcp-label">args</span>
            <code class="mcp-code">{{ mcpArgs(item).join(' ') }}</code>
          </div>
        </div>

        <div class="card-meta">
          <Tag v-if="type === 'skill' && execType(item)">
            {{ execType(item) }}
          </Tag>
          <Tag
            v-if="type === 'agent'"
            :color="toolCount(item) ? 'blue' : 'default'"
          >
            {{ $t('{n} 工具', { n: toolCount(item) }) }}
          </Tag>
          <Tag>v{{ item.version || '1.0.0' }}</Tag>
        </div>

        <div class="card-actions">
          <Button
            type="primary"
            size="small"
            :loading="installingId === item.id"
            :disabled="!!item.installed"
            @click="emit('install', item)"
          >
            <template #icon>
              <CheckOutlined v-if="item.installed" />
              <DownloadOutlined v-else />
            </template>
            {{ item.installed ? $t('已安装') : $t('安装') }}
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, markRaw, type Component } from 'vue'
import { Button, Input, Tag } from 'ant-design-vue'
import {
  SearchOutlined, DownloadOutlined, CheckOutlined,
  CodeOutlined, RobotOutlined, ThunderboltOutlined,
} from '@ant-design/icons-vue'
import type { MarketItem, MarketType } from '../api'
import EmptyState from './common/EmptyState.vue'

import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const props = withDefaults(defineProps<{
  items: MarketItem[]
  type: MarketType
  installingId?: string | null
}>(), {
  installingId: null,
})

const emit = defineEmits<{
  (e: 'install', item: MarketItem): void
}>()

const searchQuery = ref('')

const typeIcons: Record<MarketType, Component> = {
  skill: CodeOutlined,
  agent: RobotOutlined,
  mcp: ThunderboltOutlined,
}

const searchPlaceholder = computed(() => {
  const map: Record<MarketType, string> = {
    skill: t('搜索技能…'),
    agent: t('搜索 Agent…'),
    mcp: t('搜索 MCP…'),
  }
  return map[props.type] || t('搜索市场…')
})

/**
 * manifest 里前端真正用到的字段。
 *
 * 后端给的是自由对象（且可能是 JSON 字符串），原先这里用 `Record<string, any>`，
 * 于是 `manifest.exec?.type` 这类访问写错了也不会报错 —— 改成声明实际读到的形状，
 * 缺失字段由各展示函数自行兜底。
 */
interface MarketManifest {
  name?: string
  description?: string
  system_prompt?: string
  command?: string
  args?: unknown
  exec?: { type?: string }
  tools?: unknown
}

/** manifest 尽力而为：可能是对象，也可能是 JSON 字符串 */
function getManifest(item: MarketItem): MarketManifest {
  const m = item?.manifest
  if (!m) return {}
  if (typeof m === 'string') {
    try { return JSON.parse(m) as MarketManifest } catch { return {} }
  }
  return m as MarketManifest
}

function displayName(item: MarketItem): string {
  return getManifest(item).name || item.name || t('未命名')
}

function displayDesc(item: MarketItem): string {
  return getManifest(item).description || t('暂无描述')
}

function systemPrompt(item: MarketItem): string {
  return getManifest(item).system_prompt || ''
}

function mcpCommand(item: MarketItem): string {
  return getManifest(item).command || ''
}

function mcpArgs(item: MarketItem): string[] {
  const args = getManifest(item).args
  return Array.isArray(args) ? (args as string[]) : []
}

function execType(item: MarketItem): string {
  return getManifest(item).exec?.type || ''
}

function toolCount(item: MarketItem): number {
  const tools = getManifest(item).tools
  return Array.isArray(tools) ? tools.length : 0
}

const filteredItems = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return props.items
  return props.items.filter(item =>
    displayName(item).toLowerCase().includes(q) ||
    displayDesc(item).toLowerCase().includes(q))
})
</script>

<style scoped>
.market-panel { display: flex; flex-direction: column; gap: 14px; }
.market-toolbar { display: flex; gap: 10px; }
.market-search { max-width: 320px; }

.market-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.market-card {
  display: flex; flex-direction: column; gap: 10px;
  padding: 16px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  transition: transform var(--dur-normal) ease, border-color var(--dur-normal) ease, box-shadow var(--dur-normal) ease;
}
.market-card:hover { transform: translateY(-2px); border-color: var(--primary); box-shadow: var(--shadow-lg); }
.market-card.installed { border-color: var(--success); }

.card-top { display: flex; align-items: flex-start; gap: 10px; }
.card-icon {
  flex: none; width: 36px; height: 36px; border-radius: 10px;
  background: var(--primary-bg); color: var(--primary);
  display: inline-flex; align-items: center; justify-content: center; font-size: 17px;
}
.card-titles { flex: 1; min-width: 0; }
.card-name { display: block; font-size: 14px; font-weight: 600; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card-desc {
  display: block; margin-top: 3px; font-size: 12px; color: var(--text-tertiary);
  line-height: 1.5; overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}
.installed-tag { flex: none; }

/* Agent：系统提示词摘要 */
.prompt-preview {
  border: 1px solid var(--border-card);
  border-radius: 8px;
  background: var(--bg-secondary);
  padding: 8px 10px;
}
.prompt-label { font-size: 10px; color: var(--text-tertiary); font-weight: 500; margin-bottom: 4px; }
.prompt-text {
  font-size: 12px; color: var(--text-secondary); line-height: 1.5;
  overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
  -webkit-line-clamp: 3; -webkit-box-orient: vertical;
}

/* MCP：命令与参数 */
.mcp-info {
  display: flex; flex-direction: column; gap: 6px;
  background: var(--bg-secondary);
  border-radius: 8px;
  padding: 8px 10px;
}
.mcp-line { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.mcp-label { flex: none; font-size: 10px; color: var(--text-tertiary); font-weight: 500; }
.mcp-code {
  font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.card-meta { display: flex; flex-wrap: wrap; gap: 4px; }
.card-actions { display: flex; justify-content: flex-end; margin-top: auto; }

@media (max-width: 640px) {
  .market-toolbar { flex-direction: column; }
  .market-search { max-width: none; width: 100%; }
  .market-grid { grid-template-columns: 1fr; }
}

@media (prefers-reduced-motion: reduce) {
  .market-card { transition: none; }
}
</style>
