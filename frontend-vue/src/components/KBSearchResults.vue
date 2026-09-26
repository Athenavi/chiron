<!-- KBSearchResults - 知识库 RAG 检索结果卡片 -->
<template>
  <div class="kb-search-results">
    <div class="results-header">
      <div class="header-title">
        <svg
          width="16"
          height="16"
          viewBox="0 0 16 16"
          fill="currentColor"
        >
          <path d="M2 2h12v12H2V2zm1 1v10h10V3H3z" />
          <path d="M5 5h6v1H5V5zm0 2h6v1H5V7zm0 2h4v1H5V9z" />
        </svg>
        {{ $t('知识库检索结果 ({n} 条)', { n: results.length }) }}
      </div>
      <div class="header-actions">
        <button
          class="btn-toggle"
          @click="toggleAll"
        >
          {{ isExpanded ? $t('common.collapse_all') : $t('common.expand_all') }}
        </button>
      </div>
    </div>
    
    <div class="results-list">
      <div
        v-for="(result, index) in results"
        :key="index"
        class="result-card"
        :class="{ expanded: isExpanded || result.expanded }"
      >
        <!-- 排名 + 相似度分数 -->
        <div class="result-rank">
          <span class="rank-badge">#{{ index + 1 }}</span>
          <span
            class="score-badge"
            :style="{ backgroundColor: getScoreColor(result.score) }"
          >
            {{ $t('相似度: {n}%', { n: (result.score * 100).toFixed(1) }) }}
          </span>
        </div>
        
        <!-- 文档标题 -->
        <div class="result-title">
          <svg
            class="doc-icon"
            width="14"
            height="14"
            viewBox="0 0 14 14"
            fill="currentColor"
          >
            <path d="M2 1h7l3 3v9H2V1zm5 0v3h3L7 1z" />
          </svg>
          <span>{{ result.documentName || $t('文档 {n}', { n: index + 1 }) }}</span>
        </div>
        
        <!-- 内容预览 -->
        <div class="result-content">
          <pre class="content-text">{{ getTruncatedContent(result.content) }}</pre>
          
          <!-- 高亮关键词 -->
          <div
            v-if="result.highlights && result.highlights.length"
            class="highlights"
          >
            <span class="highlight-label">{{ $t('common.key_snippet') }}</span>
            <span
              v-for="(highlight, hIdx) in result.highlights"
              :key="hIdx"
              class="highlight-tag"
            >
              {{ highlight }}
            </span>
          </div>
        </div>
        
        <!-- 来源信息 -->
        <div class="result-meta">
          <span class="meta-item">
            <svg
              width="12"
              height="12"
              viewBox="0 0 12 12"
              fill="currentColor"
            >
              <path d="M6 1a4 4 0 100 8 4 4 0 000-8zm0 6.5a2.5 2.5 0 110-5 2.5 2.5 0 010 5z" />
            </svg>
            Chunk #{{ result.chunkId }}
          </span>
          <span
            v-if="result.tenantId"
            class="meta-item"
          >
            {{ $t('租户: {id}', { id: result.tenantId.substring(0, 8) }) }}
          </span>
          <span
            v-if="result.timestamp"
            class="meta-item"
          >
            {{ formatTime(result.timestamp) }}
          </span>
        </div>
        
        <!-- 展开/折叠按钮 -->
        <button
          class="expand-btn"
          :class="{ collapsed: !isExpanded && !result.expanded }"
          @click.stop="toggleResult(result)"
        >
          {{ isExpanded || result.expanded ? $t('common.collapse') : $t('common.expand') }}
        </button>
      </div>
    </div>
    
    <!-- 空状态：统一 EmptyState 模式 -->
    <EmptyState
      v-if="results.length === 0"
      size="list"
      :icon="markRaw(SearchOutlined)"
      :description="$t('knowledge.no_search_results_yet')"
      :hint="$t('common.try_other_search_keywords')"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, markRaw } from 'vue'
import { SearchOutlined } from '@ant-design/icons-vue'
import EmptyState from './common/EmptyState.vue'

interface SearchResult {
  documentId: string
  chunkId: string
  content: string
  score: number
  documentName?: string
  tenantId?: string
  timestamp?: string
  highlights?: string[]
  expanded?: boolean
}

const props = withDefaults(defineProps<{
  results: SearchResult[]
  maxPreviewLength?: number
}>(), {
  maxPreviewLength: 200,
})

const emit = defineEmits<{
  (e: 'select', result: SearchResult): void
  (e: 'reopen'): void
}>()

const isExpanded = ref(false)

function toggleAll() {
  isExpanded.value = !isExpanded.value
}

function toggleResult(result: SearchResult) {
  result.expanded = !result.expanded
}

function getTruncatedContent(content: string): string {
  if (!content) return ''
  const maxLength = props.maxPreviewLength
  return content.length > maxLength
    ? content.substring(0, maxLength) + '...'
    : content
}

function getScoreColor(score: number): string {
  if (score >= 0.9) return '#10b981' // 绿色 - 极高相似
  if (score >= 0.7) return '#3b82f6' // 蓝色 - 高相似
  if (score >= 0.5) return '#f59e0b' // 橙色 - 中等
  return '#ef4444'                    // 红色 - 低相似
}

function formatTime(timestamp: string | number): string {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
</script>

<style scoped>
.kb-search-results {
  margin-top: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
}

.results-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 16px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
}

.header-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.header-actions {
  display: flex;
  gap: 8px;
}

.btn-toggle {
  min-height: 34px;
  padding: 6px 14px;
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--dur-normal);
}

.btn-toggle:hover {
  background: var(--bg-hover);
  border-color: var(--text-tertiary);
  color: var(--text-primary);
}

.results-list {
  max-height: 500px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}

.result-card {
  padding: 16px;
  border-bottom: 1px solid var(--border-card);
  transition: background var(--dur-normal);
}

.result-card:last-child {
  border-bottom: none;
}

.result-card:hover {
  background: var(--bg-hover);
}

.result-rank {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.rank-badge {
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 700;
  color: var(--on-solid);
  background: var(--text-tertiary);
  border-radius: 3px;
}

.score-badge {
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 600;
  color: var(--on-solid);
  border-radius: 3px;
}

.result-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.doc-icon {
  color: var(--text-tertiary);
  flex-shrink: 0;
}

.result-content {
  margin-bottom: 8px;
}

.content-text {
  margin: 0;
  padding: 8px 12px;
  background: var(--bg-secondary);
  border-left: 3px solid var(--border);
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 80px;
  overflow: hidden;
  transition: max-height var(--dur-normal) ease;
}

.result-card.expanded .content-text {
  max-height: 500px;
}

.highlights {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.highlight-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.highlight-tag {
  padding: 2px 8px;
  font-size: 11px;
  color: var(--on-solid);
  background: var(--primary);
  border-radius: 3px;
}

.result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.meta-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.expand-btn {
  min-height: 32px;
  padding: 5px 14px;
  font-size: 12px;
  color: var(--primary);
  background: none;
  border: 1px solid var(--primary);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--dur-normal);
}

.expand-btn:hover {
  background: var(--primary);
  color: var(--on-solid);
}

.expand-btn.collapsed {
  color: var(--text-tertiary);
  border-color: var(--border);
}

.expand-btn.collapsed:hover {
  background: var(--bg-hover);
  color: var(--text-secondary);
  border-color: var(--text-tertiary);
}

@media (max-width: 576px) {
  .result-card {
    padding: 12px;
  }
  .results-header {
    padding: 10px 12px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .result-card,
  .btn-toggle,
  .expand-btn,
  .content-text {
    transition: none;
  }
}
</style>
