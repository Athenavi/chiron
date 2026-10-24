<template>
  <Modal
    v-model:visible="modalVisible"
    :title="$t('knowledge.document_preview')"
    :footer="null"
    :style="{ maxWidth: '800px' }"
    :destroy-on-close="true"
    @cancel="close"
  >
    <Spin :spinning="loading">
      <div class="doc-preview">
        <!-- 文档元信息 -->
        <div class="doc-meta">
          <span class="doc-name">{{ document?.name || $t('knowledge.unknown_document') }}</span>
          <span class="doc-type">{{ document?.file_type?.toUpperCase() }}</span>
          <span
            v-if="document?.file_size_bytes"
            class="doc-size"
          >{{ formatSize(document.file_size_bytes) }}</span>
          <Tag
            :color="docStatusColor"
            class="doc-status"
          >
            {{ document?.status }}
          </Tag>
        </div>

        <!-- 内容预览 -->
        <div
          v-if="content"
          class="doc-content"
        >
          <div class="content-header">
            <span class="content-label">{{ $t('knowledge.document_content_preview') }}</span>
            <span class="content-chunks">{{ $t('knowledge.n_pages', { n: pageCount }) }}</span>
          </div>
          <pre class="content-body">{{ content }}</pre>
        </div>

        <EmptyState
          v-else-if="!loading"
          size="list"
          :description="$t('common.no_content_preview_yet')"
          :hint="$t('errors.the_document_may_be_processing_or_its_format_is_not_previewable_yet')"
        />

        <!-- 失败信息 -->
        <Alert
          v-if="error"
          type="error"
          :message="error"
          closable
          style="margin-top: 12px"
        />
      </div>
    </Spin>
  </Modal>
</template>

<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { Modal, Spin, Tag, Alert } from 'ant-design-vue'
import EmptyState from './common/EmptyState.vue'
import { api } from '../api'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

interface Document {
  id: string
  name: string
  file_type: string
  file_size_bytes: number
  status: string
  page_count?: number
  content?: string
}

const props = withDefaults(defineProps<{
  visible: boolean
  documentId: string | null
  kbId: string
}>(), {
  visible: false,
  documentId: null,
})

const emit = defineEmits<{
  (e: 'update:visible', v: boolean): void
}>()

// v-model 不能直接绑定 prop，使用 computed 双向绑定
const modalVisible = computed({
  get: () => props.visible,
  set: (val: boolean) => emit('update:visible', val),
})

const loading = ref(false)
const content = ref('')
const error = ref('')
const document = ref<Document | null>(null)
const pageCount = ref(0)

const docStatusColor = computed(() => {
  const s = document.value?.status
  if (s === 'completed') return 'success'
  if (s === 'processing') return 'processing'
  if (s === 'error') return 'error'
  return 'default'
})

watch(() => props.visible, async (val) => {
  if (val && props.documentId) {
    await loadPreview()
  }
})

async function loadPreview() {
  if (!props.documentId) return
  loading.value = true
  content.value = ''
  error.value = ''

  try {
    const res = await api.get(`/v1/kb/${props.kbId}/documents/${props.documentId}/preview`)
    const data = res.data?.data || res.data
    document.value = data
    content.value = data.content || ''
    pageCount.value = data.page_count || 0
  } catch (e) {
    const data = (e as { response?: { data?: { detail?: unknown; error?: unknown } } }).response?.data
    const detail = typeof data?.detail === 'string' ? data.detail : ''
    const summary = typeof data?.error === 'string' ? data.error : ''
    error.value = detail || summary || t('errors.failed_to_load_preview')
  } finally {
    loading.value = false
  }
}

function close() {
  emit('update:visible', false)
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}
</script>

<style scoped>
.doc-preview {
  padding: 4px 0;
}
.doc-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.doc-name {
  font-weight: var(--fw-semibold);
  font-size: 15px;
  color: var(--text-primary);
}
.doc-type {
  padding: 2px 8px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  background: var(--bg-secondary);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
}
.doc-size {
  font-size: 13px;
  color: var(--text-tertiary);
}
.doc-content {
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.content-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  background: var(--bg-secondary);
  border-bottom: var(--hairline-width) solid var(--border-card);
  font-size: 13px;
}
.content-label {
  font-weight: var(--fw-medium);
  color: var(--text-primary);
}
.content-chunks {
  color: var(--text-tertiary);
  font-size: 12px;
}
.content-body {
  margin: 0;
  padding: 14px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 400px;
  overflow-y: auto;
  background: var(--bg-card);
}
</style>