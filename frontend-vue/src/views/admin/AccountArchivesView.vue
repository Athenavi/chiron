<template>
  <div class="archive-view">
    <div class="page-header">
      <div>
        <h2 class="page-title page-title-sm">
          {{ $t('admin.account_archives') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.account_archives_desc') }}
        </p>
      </div>
    </div>

    <div class="query-row">
      <Input
        v-model:value="userID"
        :placeholder="$t('admin.archive_user_id_placeholder')"
        class="query-input"
        allow-clear
        @press-enter="load"
      />
      <Button
        type="primary"
        :loading="loading"
        @click="load"
      >
        {{ $t('admin.archive_query') }}
      </Button>
    </div>

    <Alert
      v-if="error"
      type="error"
      show-icon
      :message="error"
      class="query-error"
    />

    <Table
      v-if="archives.length"
      :data-source="archives"
      :columns="columns"
      :pagination="false"
      :row-key="rowKey"
      size="small"
      class="archive-table"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'size'">
          {{ formatSize(record.size) }}
        </template>
        <template v-else-if="column.key === 'action'">
          <Button
            size="small"
            :loading="downloading === record.name"
            @click="download(record as AccountArchive)"
          >
            {{ $t('admin.archive_download') }}
          </Button>
        </template>
      </template>
    </Table>

    <Empty
      v-else-if="searched && !loading"
      :description="$t('admin.archive_empty')"
    />
  </div>
</template>

<script setup lang="ts">
/**
 * 账号注销归档（平台 owner）。
 *
 * 归档是「注销到期清除前生成的个人数据快照」，写在对象存储
 * `account-archive/{user_id}/{时间戳}.json`。这里是它唯一的可视入口 ——
 * 在此之前只能用 curl 手工调接口。
 *
 * 两个刻意的设计：
 *  1. **必须先输 user_id 再查**：没有"列出全部归档"的入口。归档是已注销
 *     用户的完整个人数据，随手翻看列表本身就是一次越权风险；
 *     按账号精确查询把访问收窄到"我知道我在找谁"。
 *  2. **下载失败要能区分原因**：失败响应体也是 Blob，用 blobErrorPayload
 *     读回文本，否则用户只会看到"下载失败"，而"该归档已被生命周期策略清除"
 *     和"存储暂时不可用"需要完全不同的处置。
 */
import { computed, ref } from 'vue'
import { Alert, Button, Empty, Input, Table, message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import {
  downloadAccountArchive,
  listAccountArchives,
  type AccountArchive,
} from '../../api/admin'
import { blobErrorPayload } from '../../utils/transactionExport'

const { t } = useI18n()

const userID = ref('')
/** 本次结果对应的账号：下载时用它而不是 userID.value，
 *  避免用户查完后改了输入框导致下载到别的账号目录下。 */
const searchedID = ref('')
const loading = ref(false)
const downloading = ref('')
const archives = ref<AccountArchive[]>([])
const error = ref('')
const searched = ref(false)

// 与后端 uuidRe 同口径：这个值会被拼进对象键，前端先拦一次能少一次往返。
const uuidRe = /^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$/

const columns = computed(() => [
  { key: 'name', title: t('admin.archive_col_name'), dataIndex: 'name' },
  { key: 'size', title: t('admin.archive_col_size') },
  { key: 'modified', title: t('admin.archive_col_modified'), dataIndex: 'modified' },
  { key: 'action', title: '', width: 100 },
])

function rowKey(record: AccountArchive): string {
  return record.name
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

async function load() {
  const id = userID.value.trim()
  error.value = ''
  if (!uuidRe.test(id)) {
    error.value = t('admin.archive_invalid_user_id')
    return
  }
  loading.value = true
  archives.value = []
  try {
    archives.value = await listAccountArchives(id)
    searchedID.value = id
    searched.value = true
  } catch {
    error.value = t('admin.archive_query_failed')
  } finally {
    loading.value = false
  }
}

async function download(record: AccountArchive) {
  downloading.value = record.name
  try {
    const blob = await downloadAccountArchive(searchedID.value, record.name)
    triggerDownload(blob, `chiron-archive-${record.name}`)
  } catch (e) {
    message.error((await extractBlobError(e)) || t('admin.archive_download_failed'))
  } finally {
    downloading.value = ''
  }
}

/** 从 blob 错误响应里取可读文案；取不到返回空串让调用方回退。 */
async function extractBlobError(e: unknown): Promise<string> {
  const data = (e as { response?: { data?: unknown } })?.response?.data
  if (data instanceof Blob) {
    const payload = await blobErrorPayload(data)
    if (payload?.message) return payload.message
  }
  return ''
}

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  // 必须释放：否则 blob 会一直占着内存直到页面关闭。
  URL.revokeObjectURL(url)
}
</script>

<style scoped>
.archive-view { padding: 4px; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-secondary); }
.query-row { display: flex; gap: 8px; margin: 16px 0; }
.query-input { max-width: 420px; }
.query-error { margin-bottom: 12px; }
.archive-table { margin-top: 4px; }
</style>
