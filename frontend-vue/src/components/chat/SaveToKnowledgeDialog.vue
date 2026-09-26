<script setup lang="ts">
/**
 * 把对话沉淀成知识库文档。
 *
 * 不新增接口：内容包成 Markdown 的 `File`，复用知识库既有的分片上传链路
 * （`createChunkUpload`，与知识库页上传完全同一条路），因此大对话也不会被单次请求限制卡住。
 */
import { computed, ref, watch } from 'vue'
import { Input, Modal, Select, message } from 'ant-design-vue'
import { listKnowledgeBases, type WorkbenchResource } from '../../api'
import { createChunkUpload } from '../../utils/uploader'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const props = defineProps<{
  open: boolean
  /** 要沉淀的内容（Markdown 文本） */
  content: string
  /** 默认标题（一般用会话标题） */
  defaultTitle?: string
}>()
const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'saved', kbId: string): void
}>()

const bases = ref<WorkbenchResource[]>([])
const loadingBases = ref(false)
const kbId = ref<string | undefined>(undefined)
const title = ref('')
const saving = ref(false)
const percent = ref(0)

watch(() => props.open, async (open) => {
  if (!open) return
  percent.value = 0
  title.value = props.defaultTitle?.trim() || t('对话记录 {time}', { time: new Date().toLocaleString() })
  if (bases.value.length > 0) return
  loadingBases.value = true
  try {
    bases.value = await listKnowledgeBases()
  } catch {
    bases.value = []
  } finally {
    loadingBases.value = false
  }
})

const options = computed(() => bases.value.map(b => ({ value: b.id, label: b.name })))
const canSave = computed(() => !!kbId.value && props.content.trim().length > 0 && !saving.value)

/** 文件名里不能出现的字符统一替换掉（否则部分后端会拒收） */
function safeFileName(): string {
  const base = (title.value || t('chat.conversation')).replace(/[\\/:*?"<>|]/g, '_').trim().slice(0, 80)
  return `${base || t('chat.conversation')}.md`
}

async function save() {
  if (!canSave.value || !kbId.value) return
  saving.value = true
  try {
    const file = new File([props.content], safeFileName(), { type: 'text/markdown' })
    const handle = await createChunkUpload(file, { purpose: 'kb_doc', parentId: kbId.value })
    handle.onProgress(pct => { percent.value = pct })
    await handle.done
    message.success(t('knowledge.stored_in_knowledge_base_becomes_searchable_after_indexing_on_the_knowledge_base_page'))
    emit('saved', kbId.value)
    emit('update:open', false)
  } catch (error) {
    message.error(t('存入失败：{error}', { error: error instanceof Error ? error.message : String(error) }))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal
    :open="open"
    :title="$t('knowledge.save_to_knowledge_base')"
    :confirm-loading="saving"
    :ok-button-props="{ disabled: !canSave }"
    :ok-text="$t('common.upload')"
    :cancel-text="$t('common.cancel')"
    @ok="save"
    @cancel="emit('update:open', false)"
  >
    <div class="save-kb">
      <div class="save-kb-field">
        <label class="save-kb-label">{{ $t('knowledge.target_knowledge_base') }}</label>
        <Select
          v-model:value="kbId"
          :options="options"
          :loading="loadingBases"
          :placeholder="$t('knowledge.select_a_knowledge_base')"
          show-search
          option-filter-prop="label"
          class="save-kb-control"
        />
      </div>
      <div class="save-kb-field">
        <label class="save-kb-label">{{ $t('knowledge.document_title') }}</label>
        <Input
          v-model:value="title"
          :placeholder="$t('knowledge.used_as_file_name_and_retrieval_source')"
        />
      </div>
      <p class="save-kb-hint">
        {{ $t('将上传本次对话的正文（{n} 字符）为 Markdown 文档；思考过程与工具调用不会写入。', { n: content.length }) }}
      </p>
      <p
        v-if="saving"
        class="save-kb-hint"
      >
        {{ $t('上传中 {n}%', { n: percent }) }}
      </p>
    </div>
  </Modal>
</template>

<style scoped>
.save-kb-field {
  margin-bottom: var(--space-3);
}

.save-kb-label {
  display: block;
  margin-bottom: var(--space-1);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-secondary);
}

.save-kb-control {
  width: 100%;
}

.save-kb-hint {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--text-secondary);
}
</style>
