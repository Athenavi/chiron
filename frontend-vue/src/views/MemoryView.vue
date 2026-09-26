<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, markRaw } from 'vue'
import { useRouter } from 'vue-router'
import {
  Card,
  Button,
  Input,
  
  Slider,
  Select,
  Tabs,
  TabPane,
  List,
  Tag,
  Popconfirm,
  Spin,
  Modal,
  message,
  Badge,
  Alert,
  Tooltip,
  Space,
  Switch,
} from 'ant-design-vue'
import {
  PlusOutlined,
  DeleteOutlined,
  EditOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  DatabaseOutlined,
  CommentOutlined,
} from '@ant-design/icons-vue'
import EmptyState from '../components/common/EmptyState.vue'
import ConflictCard from '../components/memory/ConflictCard.vue'
import {
  listMemory,
  upsertMemory,
  updateMemory,
  deleteMemory,
  clearMemory,
  searchMemory,
  startOrganize,
  getOrganizeStatus,
  listConflicts,
  listSummaries,
  MEMORY_SLOTS,
  type MemoryEntry,
  type MemorySearchHit,
  type MemorySlot,
  type MemorySource,
  type OrganizeStatus,
  type MemorySearchSummary,
  type MemoryConflict,
  type MemorySummary,
} from '../api/memory'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const router = useRouter()

const loading = ref(false)
const error = ref(false)
const entries = ref<MemoryEntry[]>([])
const counts = ref<Record<string, number>>({})
const total = ref(0)
const conflicts = ref<MemoryConflict[]>([])
const summaries = ref<MemorySummary[]>([])
const includeArchived = ref(false)

/** 浏览分区：all / 四个槽位 / 待裁决 / 摘要 */
type BrowseTab = 'all' | MemorySlot | 'conflicts' | 'summaries'
const activeTab = ref<BrowseTab>('all')
/** 只有 all 与四个槽位是「条目」分区，另两个渲染各自主角 */
const isEntryTab = computed(
  () => activeTab.value !== 'conflicts' && activeTab.value !== 'summaries',
)
const filteredEntries = computed(() =>
  activeTab.value === 'all' ? entries.value : entries.value.filter((e) => e.slot === activeTab.value),
)

// ── 语义检索 ──
const searchInput = ref('')
const searching = ref(false)
const searchingMode = ref<'semantic' | 'keyword'>('semantic')
const searchResults = ref<MemorySearchHit[] | null>(null)
const searchSummaries = ref<MemorySearchSummary[]>([])

async function handleSearch() {
  const q = searchInput.value.trim()
  if (!q) {
    clearSearch()
    return
  }
  searching.value = true
  try {
    const data = await searchMemory(q, { top_k: 10 })
    searchingMode.value = data.mode
    searchResults.value = data.results ?? []
    // L3 区做兜底：后端尚未升级到双区契约时，不该让整个检索崩在 .length 上。
    searchSummaries.value = data.summaries ?? []
    if (searchResults.value.length === 0 && searchSummaries.value.length === 0) {
      message.info(t('errors.no_similar_memory_found'))
    }
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.search_failed'))
  } finally {
    searching.value = false
  }
}

function clearSearch() {
  searchInput.value = ''
  searchResults.value = null
  searchSummaries.value = []
}

// ── 增删改 ──
const formVisible = ref(false)
const saving = ref(false)
const editing = ref<MemoryEntry | null>(null)
const form = ref<{
  slot: MemorySlot
  key: string
  value: string
  confidence: number
  source: MemorySource
}>({
  slot: 'fact',
  key: '',
  value: '',
  confidence: 50,
  source: 'user_confirmed',
})

function openCreate(slot?: MemorySlot) {
  editing.value = null
  form.value = { slot: slot ?? 'fact', key: '', value: '', confidence: 50, source: 'user_confirmed' }
  formVisible.value = true
}

function openEdit(entry: MemoryEntry) {
  editing.value = entry
  form.value = {
    slot: entry.slot as MemorySlot,
    key: entry.key,
    value: entry.value,
    confidence: entry.confidence,
    source: entry.source as MemorySource,
  }
  formVisible.value = true
}

async function handleSave() {
  const { slot, key, value, confidence, source } = form.value
  if (!key.trim() || !value.trim()) {
    message.warning(t('common.key_and_content_cannot_be_empty'))
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await updateMemory({ id: editing.value.id, key, value, confidence, source })
      message.success(t('memory.memory_updated'))
    } else {
      const res = await upsertMemory({ slot, key, value, confidence, source })
      if (res.duplicate_of) {
        message.warning(t('检测到相似记忆「{key}」，可在整理时自动合并', { key: res.duplicate_of.key }))
      } else {
        message.success(t('memory.memory_saved'))
      }
    }
    formVisible.value = false
    await loadProfile()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.save_failed'))
  } finally {
    saving.value = false
  }
}

async function handleDelete(id: string) {
  try {
    await deleteMemory(id)
    message.success(t('common.deleted'))
    await loadProfile()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.delete_failed'))
  }
}

async function handleClearAll() {
  try {
    const res = await clearMemory()
    message.success(t('已清空 {n} 条记忆', { n: res.deleted }))
    await loadProfile()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.failed_to_clear'))
  }
}

// ── 智能整理（异步 + 轮询） ──
const organize = ref<OrganizeStatus>({
  running: false,
  started_at: null,
  finished_at: null,
  result: null,
  error: null,
})
let organizeTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (organizeTimer) {
    clearInterval(organizeTimer)
    organizeTimer = null
  }
}

async function pollOrganize() {
  stopPolling()
  organizeTimer = setInterval(async () => {
    try {
      const st = await getOrganizeStatus()
      organize.value = st
      if (!st.running) {
        stopPolling()
        if (st.result) {
          const r = st.result
          const parts: string[] = []
          if (r.merged) parts.push(t('合并 {n} 条重复', { n: r.merged }))
          if (r.backfilled) parts.push(t('补齐 {n} 条向量', { n: r.backfilled }))
          if (r.archived) parts.push(t('归档 {n} 条', { n: r.archived }))
          if (r.evicted) parts.push(t('淘汰 {n} 条', { n: r.evicted }))
          if (parts.length) message.success(t('common.organization_complete') + parts.join('，'))
          else message.success(t('common.organization_complete_no_adjustment_needed'))
          await loadProfile()
        } else if (st.error) {
          message.error(t('errors.organization_failed') + st.error)
        }
      }
    } catch {
      stopPolling()
    }
  }, 1200)
}

async function runOrganize() {
  try {
    const res = await startOrganize()
    organize.value = res.status
    if (res.started) {
      message.info(t('common.smart_organization_started_runs_in_background'))
      pollOrganize()
    } else {
      message.info(t('workflow.an_organization_task_is_already_running'))
      pollOrganize()
    }
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.failed_to_start_organization'))
  }
}

// ── 加载 ──
async function loadProfile() {
  loading.value = true
  error.value = false
  try {
    const data = await listMemory(includeArchived.value)
    entries.value = data.entries || []
    counts.value = data.counts || {}
    total.value = data.total
    organize.value = data.organize
  } catch (e: any) {
    error.value = true
    if (e.response?.status === 503) {
      message.error(t('errors.memory_service_unavailable_postgresql_required'))
    } else {
      message.error(e.response?.data?.error || t('errors.failed_to_load_memory'))
    }
  } finally {
    loading.value = false
  }
}

async function loadConflicts() {
  try {
    const data = await listConflicts()
    conflicts.value = data.conflicts || []
  } catch {
    // 冲突列表不可用不该影响记忆条目浏览
    conflicts.value = []
  }
}

async function loadSummaries() {
  try {
    const data = await listSummaries()
    summaries.value = data.summaries || []
  } catch {
    summaries.value = []
  }
}

/** 裁决或忽略后从待办里移除（后端已消化，重复展示会让用户以为没生效） */
function onConflictResolved(conflictId: string) {
  conflicts.value = conflicts.value.filter((c) => c.conflict_id !== conflictId)
}

/**
 * 带着这条记忆的分类跳到对话（`?memory=<slot>`）：对话侧据此收窄注入范围。
 * 单条记忆不单独传 id —— 注入粒度是「分类」，传 id 会让后端需要另一套白名单语义。
 */
function useSlotInChat(slot: string) {
  void router.push({ path: '/chat', query: { memory: slot } })
}

// antd Switch 的 change 回调首个参数是 CheckedType（boolean | string | number）
async function onArchivedChange(value: boolean | string | number) {
  includeArchived.value = Boolean(value)
  await loadProfile()
}

onMounted(() => {
  void loadProfile()
  void loadConflicts()
  void loadSummaries()
})
onUnmounted(stopPolling)

function slotLabel(slot: string): string {
  return MEMORY_SLOTS.find((s) => s.slot === slot)?.label ?? slot
}
function pct(n: number): string {
  return Math.round(n * 100) + '%'
}
</script>

<template>
  <div class="memory-view">
    <div class="page-header">
      <div class="title">
        <DatabaseOutlined />
        <span>{{ $t('memory.long_term_memory') }}</span>
        <Badge
          :count="total"
          :overflow-count="999"
          color="var(--node-indigo)"
        />
        <span class="subtitle">{{ $t('knowledge.cross_session_retention_semantic_retrieval_auto_organization') }}</span>
      </div>
      <Space>
        <Switch
          :checked="includeArchived"
          :checked-children="$t('common.include_archived')"
          :un-checked-children="$t('common.active_only')"
          @change="onArchivedChange"
        />
        <Button
          :loading="organize.running"
          @click="runOrganize"
        >
          <template #icon>
            <ThunderboltOutlined />
          </template>
          {{ $t('common.smart_organization') }}
        </Button>
        <Popconfirm
          :title="$t('memory.confirm_clearing_all_long_term_memory_this_action_cannot_be_undone')"
          :ok-text="$t('common.empty')"
          :cancel-text="$t('common.cancel')"
          @confirm="handleClearAll"
        >
          <Button danger>
            {{ $t('memory.clear_memory') }}
          </Button>
        </Popconfirm>
        <Button
          type="primary"
          @click="openCreate()"
        >
          <template #icon>
            <PlusOutlined />
          </template>
          {{ $t('memory.new_memory') }}
        </Button>
      </Space>
    </div>

    <!-- 整理状态 -->
    <Alert
      v-if="organize.running"
      type="info"
      show-icon
      :message="$t('knowledge.cleaning_up_memory_in_the_background_dedupe_archive_backfill_vectors')"
      style="margin-bottom: 16px"
    />
    <Alert
      v-else-if="organize.result"
      type="success"
      show-icon
      style="margin-bottom: 16px"
      :message="$t('上次整理：合并 {merged} · 补齐 {backfilled} · 归档 {archived} · 淘汰 {evicted}', { merged: organize.result.merged, backfilled: organize.result.backfilled, archived: organize.result.archived, evicted: organize.result.evicted })"
    />

    <!-- 语义检索 -->
    <Card
      class="search-card"
      :bordered="true"
    >
      <Space
        style="width: 100%"
        wrap
      >
        <Input
          v-model:value="searchInput"
          :placeholder="$t('knowledge.semantic_retrieval_e_g_what_editor_does_the_user_prefer')"
          style="width: 360px"
          allow-clear
          @press-enter="handleSearch"
          @search="handleSearch"
        >
          <template #prefix>
            <SearchOutlined />
          </template>
        </Input>
        <Button
          type="primary"
          :loading="searching"
          @click="handleSearch"
        >
          {{ $t('knowledge.smart_retrieval') }}
        </Button>
        <Button
          v-if="searchResults !== null"
          @click="clearSearch"
        >
          {{ $t('common.back_to_list') }}
        </Button>
        <Tag
          v-if="searchResults !== null"
          :color="searchingMode === 'semantic' ? 'blue' : 'orange'"
        >
          {{ searchingMode === 'semantic' ? $t('common.semantic_mode') : $t('common.keyword_mode') }}
        </Tag>
      </Space>
    </Card>

    <!-- 检索结果 -->
    <div
      v-if="searchResults !== null"
      class="results"
    >
      <h3
        v-if="searchResults.length"
        class="result-section-title"
      >
        {{ $t('记忆条目（{n}）', { n: searchResults.length }) }}
      </h3>
      <List
        :data-source="searchResults"
        :locale="{ emptyText: $t('errors.no_similar_memory_found') }"
      >
        <template #renderItem="{ item }">
          <List.Item>
            <Card
              class="entry-card"
              size="small"
              style="width: 100%"
            >
              <div class="entry-head">
                <Tag color="purple">
                  {{ slotLabel(item.slot) }}
                </Tag>
                <span class="entry-key">{{ item.key }}</span>
                <Tooltip :title="$t('相关度 {rel} · 重排序分 {score}', { rel: pct(item.similarity), score: item.score.toFixed(2) })">
                  <Tag color="green">
                    {{ pct(item.score) }}
                  </Tag>
                </Tooltip>
              </div>
              <div class="entry-value">
                {{ item.value }}
              </div>
            </Card>
          </List.Item>
        </template>
      </List>

      <h3
        v-if="searchSummaries.length"
        class="result-section-title"
      >
        {{ $t('历史对话（{n}）', { n: searchSummaries.length }) }}
      </h3>
      <List
        :data-source="searchSummaries"
        :locale="{ emptyText: $t('errors.no_related_conversation_found') }"
      >
        <template #renderItem="{ item }">
          <List.Item>
            <Card
              class="entry-card"
              size="small"
              style="width: 100%"
            >
              <div class="entry-head">
                <Tag color="cyan">
                  {{ $t('chat.history_conversations') }}
                </Tag>
                <Tag
                  v-if="item.topics && item.topics.length"
                  color="default"
                >
                  {{ item.topics.slice(0, 3).join(' · ') }}
                </Tag>
              </div>
              <div class="entry-value">
                {{ item.content }}
              </div>
            </Card>
          </List.Item>
        </template>
      </List>

      <EmptyState
        v-if="!searchResults.length && !searchSummaries.length"
        size="page"
        :icon="markRaw(SearchOutlined)"
        :description="$t('errors.no_similar_memory_found')"
        :hint="$t('memory.try_rephrasing_or_create_memory_above_first')"
      />
    </div>

    <!-- 浏览（按槽位 Tab） -->
    <Spin
      v-else
      :spinning="loading"
    >
      <Tabs v-model:active-key="activeTab">
        <TabPane key="all">
          <template #tab>
            {{ $t('全部 ({n})', { n: total }) }}
          </template>
        </TabPane>
        <TabPane
          v-for="s in MEMORY_SLOTS"
          :key="s.slot"
        >
          <template #tab>
            {{ s.label }} ({{ counts[s.slot] || 0 }})
          </template>
        </TabPane>
        <TabPane key="conflicts">
          <template #tab>
            {{ $t('待裁决 ({n})', { n: conflicts.length }) }}
          </template>
        </TabPane>
        <TabPane key="summaries">
          <template #tab>
            {{ $t('摘要 ({n})', { n: summaries.length }) }}
          </template>
        </TabPane>
      </Tabs>

      <!-- 条目浏览（全部 / 分类） -->
      <template v-if="isEntryTab">
        <EmptyState
          v-if="error && !loading"
          size="page"
          :icon="markRaw(DatabaseOutlined)"
          :description="$t('errors.failed_to_load')"
          :hint="$t('memory.unable_to_fetch_memory_data_please_retry_later')"
        >
          <Button
            type="primary"
            @click="loadProfile"
          >
            {{ $t('common.retry') }}
          </Button>
        </EmptyState>

        <EmptyState
          v-else-if="filteredEntries.length === 0 && !loading"
          size="page"
          :icon="markRaw(DatabaseOutlined)"
          :description="$t('memory.no_memory_in_this_category_yet')"
          :hint="$t('memory.click_new_memory_at_the_top_right_to_record_user_preferences_and_long_term_context')"
        />

        <List
          v-else
          :data-source="filteredEntries"
        >
          <template #renderItem="{ item }">
            <List.Item>
              <Card
                class="entry-card"
                size="small"
                style="width: 100%"
              >
                <div class="entry-head">
                  <Tag color="purple">
                    {{ item.slot_label }}
                  </Tag>
                  <span class="entry-key">{{ item.key }}</span>
                  <Tag :color="item.source === 'user_confirmed' ? 'green' : 'default'">
                    {{ item.source_label }}
                  </Tag>
                  <Tooltip :title="$t('common.confidence_high_confidence_items_are_kept_first_during_organization')">
                    <Tag color="blue">
                      {{ $t('置信 {n}', { n: item.confidence }) }}
                    </Tag>
                  </Tooltip>
                  <Space class="entry-actions">
                    <Button
                      size="small"
                      type="text"
                      :title="$t('memory.use_this_type_of_memory_in_the_conversation')"
                      @click="useSlotInChat(item.slot)"
                    >
                      <template #icon>
                        <CommentOutlined />
                      </template>
                    </Button>
                    <Button
                      size="small"
                      type="text"
                      :title="$t('common.edit_2')"
                      @click="openEdit(item)"
                    >
                      <template #icon>
                        <EditOutlined />
                      </template>
                    </Button>
                    <Popconfirm
                      :title="$t('memory.delete_this_memory')"
                      :ok-text="$t('common.delete')"
                      :cancel-text="$t('common.cancel')"
                      @confirm="handleDelete(item.id)"
                    >
                      <Button
                        size="small"
                        type="text"
                        danger
                        :title="$t('common.delete')"
                      >
                        <template #icon>
                          <DeleteOutlined />
                        </template>
                      </Button>
                    </Popconfirm>
                  </Space>
                </div>
                <div class="entry-value">
                  {{ item.value }}
                </div>
                <div class="entry-meta">
                  {{ $t('访问 {n} 次 · 更新于 {date}', { n: item.access_count, date: item.updated_at?.slice(0, 10) }) }}
                </div>
              </Card>
            </List.Item>
          </template>
        </List>
      </template>

      <!-- 待裁决冲突 -->
      <template v-else-if="activeTab === 'conflicts'">
        <EmptyState
          v-if="conflicts.length === 0"
          size="page"
          :icon="markRaw(DatabaseOutlined)"
          :description="$t('errors.no_pending_memory_conflicts_to_resolve')"
          :hint="$t('errors.when_the_content_the_ai_distilled_conflicts_with_info_you_confirmed_it_shows_up_here_for_your_decision')"
        />
        <ConflictCard
          v-for="c in conflicts"
          :key="c.conflict_id"
          :conflict="c"
          @resolved="onConflictResolved"
          @dismissed="onConflictResolved"
        />
      </template>

      <!-- L3 对话摘要 -->
      <template v-else>
        <EmptyState
          v-if="summaries.length === 0"
          size="page"
          :icon="markRaw(DatabaseOutlined)"
          :description="$t('chat.no_conversation_summary_yet')"
          :hint="$t('chat.when_a_conversation_ends_the_dialogue_is_automatically_distilled_into_a_summary_for_cross_session_recall')"
        />
        <List
          v-else
          :data-source="summaries"
        >
          <template #renderItem="{ item }">
            <List.Item>
              <Card
                class="entry-card"
                size="small"
                style="width: 100%"
              >
                <div class="entry-head">
                  <Tag color="cyan">
                    {{ $t('chat.history_conversations') }}
                  </Tag>
                  <Tag
                    v-if="item.topics && item.topics.length"
                    color="default"
                  >
                    {{ item.topics.slice(0, 3).join(' · ') }}
                  </Tag>
                </div>
                <div class="entry-value">
                  {{ item.content }}
                </div>
              </Card>
            </List.Item>
          </template>
        </List>
      </template>
    </Spin>

    <!-- 新建 / 编辑弹窗 -->
    <Modal
      v-model:open="formVisible"
      :title="editing ? $t('memory.edit_memory') : $t('memory.new_memory')"
      :confirm-loading="saving"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      @ok="handleSave"
    >
      <div class="form-row">
        <label>{{ $t('common.category') }}</label>
        <Select
          v-model:value="form.slot"
          style="width: 100%"
        >
          <Select.Option
            v-for="s in MEMORY_SLOTS"
            :key="s.slot"
            :value="s.slot"
          >
            {{ s.label }}
          </Select.Option>
        </Select>
      </div>
      <div class="form-row">
        <label>{{ $t('common.key_2') }}</label>
        <Input
          v-model:value="form.key"
          :placeholder="$t('common.e_g_timezone_stack_rejected_needs')"
        />
      </div>
      <div class="form-row">
        <label>{{ $t('common.content_value') }}</label>
        <Input.TextArea
          v-model:value="form.value"
          :rows="3"
          :placeholder="$t('memory.memory_content')"
        />
      </div>
      <div class="form-row">
        <label>{{ $t('置信度 {n}', { n: form.confidence }) }}</label>
        <Slider
          v-model:value="form.confidence"
          :min="0"
          :max="100"
        />
      </div>
      <div class="form-row">
        <label>{{ $t('common.source') }}</label>
        <Select
          v-model:value="form.source"
          style="width: 100%"
        >
          <Select.Option value="user_confirmed">
            {{ $t('admin.user_confirmation') }}
          </Select.Option>
          <Select.Option value="derived">
            {{ $t('chat.conversation_distillation') }}
          </Select.Option>
          <Select.Option value="tool_written">
            {{ $t('agent.tool_write') }}
          </Select.Option>
        </Select>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.memory-view {
  max-width: 920px;
  margin: 0 auto;
  padding: 28px 24px 60px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 20px;
}
.title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.title .subtitle {
  font-size: 12px;
  font-weight: 400;
  color: var(--text-secondary, #888);
}
.search-card {
  margin-bottom: 16px;
}
.result-section-title {
  margin: 18px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary, #888);
}
.entry-card {
  margin-bottom: 8px;
}
.entry-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.entry-key {
  font-weight: 600;
}
.entry-actions {
  margin-left: auto;
}
.entry-value {
  margin: 6px 0;
  white-space: pre-wrap;
  line-height: 1.5;
}
.entry-meta {
  font-size: 12px;
  color: var(--text-secondary, #888);
}

/* 移动端：检索框占满、标题区换行 */
@media (max-width: 768px) {
  .memory-view {
    padding: 20px 16px 48px;
  }
  .search-card :deep(.ant-input-affix-wrapper) {
    width: 100% !important;
  }
  .title {
    font-size: 20px;
  }
  .title .subtitle {
    display: none;
  }
  .entry-head {
    row-gap: 4px;
  }
}
</style>
