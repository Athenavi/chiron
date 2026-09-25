<template>
  <Modal
    v-model:visible="modalVisible"
    :title="$t('文档预览')"
    :footer="null"
    :style="{ maxWidth: '800px' }"
    :destroy-on-close="true"
    @cancel="close"
  >
    <Spin :spinning="loading">
      <div class="doc-preview">
        <!-- 文档元信息 -->
        <div class="doc-meta">
          <span class="doc-name">{{ document?.name || $t('未知文档') }}</span>
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
            <span class="content-label">{{ $t('文档内容预览') }}</span>
            <span class="content-chunks">{{ $t('{n} 个分块', { n: chunkCount }) }}</span>
          </div>
          <pre class="content-body">{{ content }}</pre>
        </div>

        <EmptyState
          v-else-if="!loading"
          size="list"
          :description="$t('暂无内容预览')"
          :hint="$t('文档可能正在处理中，或内容格式暂不支持预览')"
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
  chunk_count?: number
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
const chunkCount = ref(0)

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
    chunkCount.value = data.chunk_count || 0
  } catch (e: any) {
    error.value = e.response?.data?.detail || e.response?.data?.error || t('加载预览失败')
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
  font-weight: 600;
  font-size: 15px;
  color: var(--text-primary);
}
.doc-type {
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 600;
  background: var(--bg-secondary);
  border-radius: 4px;
  color: var(--text-secondary);
}
.doc-size {
  font-size: 13px;
  color: var(--text-tertiary);
}
.doc-content {
  border: 1px solid var(--border-card);
  border-radius: 8px;
  overflow: hidden;
}
.content-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-card);
  font-size: 13px;
}
.content-label {
  font-weight: 500;
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