<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, markRaw } from 'vue'
import { useRouter } from 'vue-router'
import {
  Card,
  Button,
  Input,
  InputNumber,
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
      message.info(t('未找到相似记忆'))
    }
  } catch (e: any) {
    message.error(e.response?.data?.error || t('检索失败'))
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
    message.warning(t('键与内容均不能为空'))
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await updateMemory({ id: editing.value.id, key, value, confidence, source })
      message.success(t('已更新记忆'))
    } else {
      const res = await upsertMemory({ slot, key, value, confidence, source })
      if (res.duplicate_of) {
        message.warning(`检测到相似记忆「${res.duplicate_of.key}」，可在整理时自动合并`)
      } else {
        message.success(t('已保存记忆'))
      }
    }
    formVisible.value = false
    await loadProfile()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('保存失败'))
  } finally {
    saving.value = false
  }
}

async function handleDelete(id: string) {
  try {
    await deleteMemory(id)
    message.success(t('已删除'))
    await loadProfile()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('删除失败'))
  }
}

async function handleClearAll() {
  try {
    const res = await clearMemory()
    message.success(`已清空 ${res.deleted} 条记忆`)
    await loadProfile()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('清空失败'))
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
          if (r.merged) parts.push(`合并 ${r.merged} 条重复`)
          if (r.backfilled) parts.push(`补齐 ${r.backfilled} 条向量`)
          if (r.archived) parts.push(`归档 ${r.archived} 条`)
          if (r.evicted) parts.push(`淘汰 ${r.evicted} 条`)
          if (parts.length) message.success(t('整理完成：') + parts.join('，'))
          else message.success(t('整理完成：无需调整'))
          await loadProfile()
        } else if (st.error) {
          message.error(t('整理失败：') + st.error)
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
      message.info(t('智能整理已启动（后台运行）'))
      pollOrganize()
    } else {
      message.info(t('已有整理任务在运行中'))
      pollOrganize()
    }
  } catch (e: any) {
    message.error(e.response?.data?.error || t('启动整理失败'))
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
      message.error(t('记忆服务不可用（需要 PostgreSQL）'))
    } else {
      message.error(e.response?.data?.error || t('加载记忆失败'))
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
        <span>{{ $t('长期记忆') }}</span>
        <Badge
          :count="total"
          :overflow-count="999"
          color="var(--node-indigo)"
        />
        <span class="subtitle">{{ $t('跨会话留存 · 语义检索 · 自动整理') }}</span>
      </div>
      <Space>
        <Switch
          :checked="includeArchived"
          checked-children="含归档"
          un-checked-children="仅活跃"
          @change="onArchivedChange"
        />
        <Button
          :loading="organize.running"
          @click="runOrganize"
        >
          <template #icon>
            <ThunderboltOutlined />
          </template>
          {{ $t('智能整理') }}
        </Button>
        <Popconfirm
          :title="$t('确定清空全部长期记忆？此操作不可恢复')"
          :ok-text="$t('清空')"
          :cancel-text="$t('取消')"
          @confirm="handleClearAll"
        >
          <Button danger>
            {{ $t('清空记忆') }}
          </Button>
        </Popconfirm>
        <Button
          type="primary"
          @click="openCreate()"
        >
          <template #icon>
            <PlusOutlined />
          </template>
          {{ $t('新建记忆') }}
        </Button>
      </Space>
    </div>

    <!-- 整理状态 -->
    <Alert
      v-if="organize.running"
      type="info"
      show-icon
      message="正在后台整理记忆（去重 / 归档 / 补齐向量）…"
      style="margin-bottom: 16px"
    />
    <Alert
      v-else-if="organize.result"
      type="success"
      show-icon
      style="margin-bottom: 16px"
      :message="`上次整理：合并 ${organize.result.merged} · 补齐 ${organize.result.backfilled} · 归档 ${organize.result.archived} · 淘汰 ${organize.result.evicted}`"
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
          :placeholder="$t('语义检索：如「用户偏好用什么编辑器」')"
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
          {{ $t('智能检索') }}
        </Button>
        <Button
          v-if="searchResults !== null"
          @click="clearSearch"
        >
          {{ $t('返回列表') }}
        </Button>
        <Tag
          v-if="searchResults !== null"
          :color="searchingMode === 'semantic' ? 'blue' : 'orange'"
        >
          {{ searchingMode === 'semantic' ? '语义模式' : '关键词模式' }}
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
        记忆条目（{{ searchResults.length }}）
      </h3>
      <List
        :data-source="searchResults"
        :locale="{ emptyText: '未找到相似记忆' }"
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
                <Tooltip :title="`相关度 ${pct(item.similarity)} · 重排序分 ${item.score.toFixed(2)}`">
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
        历史对话（{{ searchSummaries.length }}）
      </h3>
      <List
        :data-source="searchSummaries"
        :locale="{ emptyText: '未找到相关历史对话' }"
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
                  {{ $t('历史对话') }}
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
        :description="$t('未找到相似记忆')"
        :hint="$t('换一个说法再试，或先在上方新建记忆')"
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
            全部 ({{ total }})
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
            待裁决 ({{ conflicts.length }})
          </template>
        </TabPane>
        <TabPane key="summaries">
          <template #tab>
            摘要 ({{ summaries.length }})
          </template>
        </TabPane>
      </Tabs>

      <!-- 条目浏览（全部 / 分类） -->
      <template v-if="isEntryTab">
      <EmptyState
        v-if="error && !loading"
        size="page"
        :icon="markRaw(DatabaseOutlined)"
        :description="$t('加载失败')"
        :hint="$t('无法获取记忆数据，请稍后重试')"
      >
        <Button
          type="primary"
          @click="loadProfile"
        >
          {{ $t('重试') }}
        </Button>
      </EmptyState>

      <EmptyState
        v-else-if="filteredEntries.length === 0 && !loading"
        size="page"
        :icon="markRaw(DatabaseOutlined)"
        :description="$t('暂无该分类记忆')"
        :hint="$t('点击右上角「新建记忆」，记录用户偏好与长期上下文')"
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
                <Tooltip :title="$t('置信度（整理时高置信条目优先保留）')">
                  <Tag color="blue">
                    置信 {{ item.confidence }}
                  </Tag>
                </Tooltip>
                <Space class="entry-actions">
                  <Button
                    size="small"
                    type="text"
                    :title="$t('在对话中使用这类记忆')"
                    @click="useSlotInChat(item.slot)"
                  >
                    <template #icon>
                      <CommentOutlined />
                    </template>
                  </Button>
                  <Button
                    size="small"
                    type="text"
                    :title="$t('编辑')"
                    @click="openEdit(item)"
                  >
                    <template #icon>
                      <EditOutlined />
                    </template>
                  </Button>
                  <Popconfirm
                    :title="$t('删除这条记忆？')"
                    :ok-text="$t('删除')"
                    :cancel-text="$t('取消')"
                    @confirm="handleDelete(item.id)"
                  >
                    <Button
                      size="small"
                      type="text"
                      danger
                      :title="$t('删除')"
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
                访问 {{ item.access_count }} 次 · 更新于 {{ item.updated_at?.slice(0, 10) }}
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
          :description="$t('没有待裁决的记忆冲突')"
          :hint="$t('当 AI 提炼出的内容与您确认过的信息冲突时，会出现在这里等您裁决')"
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
          :description="$t('暂无对话摘要')"
          :hint="$t('会话结束时会自动把对话提炼成摘要，用于跨会话召回')"
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
                    {{ $t('历史对话') }}
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
      :title="editing ? '编辑记忆' : '新建记忆'"
      :confirm-loading="saving"
      :ok-text="$t('保存')"
      :cancel-text="$t('取消')"
      @ok="handleSave"
    >
      <div class="form-row">
        <label>{{ $t('分类') }}</label>
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
        <label>{{ $t('键（key）') }}</label>
        <Input
          v-model:value="form.key"
          :placeholder="$t('如 timezone / stack / 拒接需求')"
        />
      </div>
      <div class="form-row">
        <label>{{ $t('内容（value）') }}</label>
        <Input.TextArea
          v-model:value="form.value"
          :rows="3"
          :placeholder="$t('记忆的具体内容')"
        />
      </div>
      <div class="form-row">
        <label>置信度 {{ form.confidence }}</label>
        <Slider
          v-model:value="form.confidence"
          :min="0"
          :max="100"
        />
      </div>
      <div class="form-row">
        <label>{{ $t('来源') }}</label>
        <Select
          v-model:value="form.source"
          style="width: 100%"
        >
          <Select.Option value="user_confirmed">
            {{ $t('用户确认') }}
          </Select.Option>
          <Select.Option value="derived">
            {{ $t('对话提炼') }}
          </Select.Option>
          <Select.Option value="tool_written">
            {{ $t('工具写入') }}
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
