<script setup lang="ts">
import { ref, onMounted, computed, markRaw } from 'vue'
import { useRouter } from 'vue-router'
import {
  Button, Modal, Input, Radio, Tag, Popconfirm, message,
} from 'ant-design-vue'
import { BookOutlined, PlusOutlined, DeleteOutlined, SearchOutlined, EditOutlined, FileTextOutlined, DatabaseOutlined, TeamOutlined, LockOutlined } from '@ant-design/icons-vue'
import { api, listAgents, setKBVisibility } from '../api'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import { collectKbUsage, kbUsageOf, type KbUsage } from '../utils/kbUsage'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
interface KnowledgeBase {
  id: string
  name: string
  description: string
  type: 'wiki' | 'rag'
  visibility: 'public' | 'private' | 'tenant'
  status: string
  document_count: number
  total_size_bytes: number
  credits_consumed: number
  created_at: string
  updated_at: string
}

const router = useRouter()
const loading = ref(true)
const knowledgeBases = ref<KnowledgeBase[]>([])
const searchQuery = ref('')

// 创建表单
const showCreateModal = ref(false)
const createForm = ref({
  name: '', description: '', type: 'wiki' as 'wiki' | 'rag', visibility: 'private' as 'public' | 'private',
})
const creating = ref(false)

// 编辑表单
const showEditModal = ref(false)
const editingKb = ref<KnowledgeBase | null>(null)
const editForm = ref({ name: '', description: '', type: 'wiki' as 'wiki' | 'rag', visibility: 'private' as 'public' | 'private' })
const saving = ref(false)
const visibilityTogglingId = ref('')

// ── 反向引用：这些知识库被哪些 Agent 当作默认知识库 ──
// 后端没有"谁在用我"的查询，而 Agent 列表前端本来就能拉（AgentsView 一直在用），
// 自己算一遍比新增一次后端往返划算。拉失败只让这一项为空，不影响知识库列表本身。
const kbUsage = ref<Map<string, KbUsage>>(new Map())

async function loadKbUsage() {
  try {
    kbUsage.value = collectKbUsage(await listAgents())
  } catch {
    kbUsage.value = new Map()
  }
}

/** 卡片上的用量摘要；没有被引用时返回空串（不显示空徽标） */
function usageLabel(kbId: string): string {
  const { agents } = kbUsageOf(kbUsage.value, kbId)
  return agents.length ? t('{n} 个 Agent', { n: agents.length }) : ''
}

/** 悬浮提示：具体是谁在用 */
function usageTitle(kbId: string): string {
  const { agents } = kbUsageOf(kbUsage.value, kbId)
  return agents.length ? `Agent：${agents.join('、')}` : ''
}

onMounted(() => {
  void loadKnowledgeBases()
  void loadKbUsage()
})

const filtered = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return knowledgeBases.value
  return knowledgeBases.value.filter(kb =>
    (kb.name || '').toLowerCase().includes(q) || (kb.description || '').toLowerCase().includes(q))
})

// tenant / 缺失均归入「我的知识库」（缺失视为 private），团队共享项靠徽标区分
const privateKbs = computed(() => filtered.value.filter(kb => kb.visibility !== 'public'))
const publicKbs = computed(() => filtered.value.filter(kb => kb.visibility === 'public'))

async function loadKnowledgeBases() {
  loading.value = true
  try {
    const res = await api.get('/v1/kb')
    // 降级：列表项缺失 visibility 时视为 private
    knowledgeBases.value = (res.data?.data?.knowledge_bases || []).map((kb: any) => ({ ...kb, visibility: kb.visibility || 'private' }))
  } catch {
    message.error(t('加载知识库失败'))
  } finally {
    loading.value = false
  }
}

async function createKnowledgeBase() {
  if (!createForm.value.name.trim()) { message.warning(t('请输入知识库名称')); return }
  creating.value = true
  try {
    await api.post('/v1/kb', createForm.value)
    message.success(t('知识库创建成功'))
    showCreateModal.value = false
    createForm.value = { name: '', description: '', type: 'wiki', visibility: 'private' }
    await loadKnowledgeBases()
  } catch (e: any) {
    message.error(e.response?.data?.detail || e.response?.data?.error || t('创建失败'))
  } finally {
    creating.value = false
  }
}

function openEdit(kb: KnowledgeBase) {
  editingKb.value = kb
  // 编辑弹窗只管理 私有/公开；团队共享态映射为 private（团队共享由独立开关切换）
  editForm.value = { name: kb.name, description: kb.description, type: kb.type, visibility: kb.visibility === 'tenant' ? 'private' : kb.visibility }
  showEditModal.value = true
}

async function saveEdit() {
  if (!editingKb.value || !editForm.value.name.trim()) { message.warning(t('请输入名称')); return }
  saving.value = true
  try {
    await api.put(`/v1/kb/${editingKb.value.id}`, editForm.value)
    message.success(t('已保存'))
    showEditModal.value = false
    await loadKnowledgeBases()
  } catch (e: any) {
    message.error(e.response?.data?.detail || e.response?.data?.error || t('保存失败'))
  } finally {
    saving.value = false
  }
}

async function deleteKnowledgeBase(id: string) {
  try {
    await api.delete(`/v1/kb/${id}`)
    message.success(t('已删除'))
    await loadKnowledgeBases()
  } catch (e: any) {
    message.error(e.response?.data?.detail || e.response?.data?.error || t('删除失败'))
  }
}

// ── 团队共享 / 私有切换（owner-only；非属主 403 提示）──
async function toggleVisibility(kb: KnowledgeBase) {
  const next = kb.visibility === 'tenant' ? 'private' : 'tenant'
  visibilityTogglingId.value = kb.id
  try {
    await setKBVisibility(kb.id, next)
    message.success(next === 'tenant' ? t('已共享给团队') : t('已设为私有'))
    await loadKnowledgeBases()
  } catch (e: any) {
    const msg = e.response?.data?.detail || e.response?.data?.error || e.response?.data?.message || ''
    if (e.response?.status === 403) {
      message.error(t('只能操作自己创建的知识库') + (msg ? `：${msg}` : ''))
    } else {
      message.error(t('操作失败') + (msg ? `：${msg}` : ''))
    }
  } finally {
    visibilityTogglingId.value = ''
  }
}

function openKnowledgeBase(kb: KnowledgeBase) {
  router.push(`/knowledge/${kb.id}`)
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}

function formatDate(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div class="kb-page">
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="page-title">
          {{ $t('知识库') }}
        </h1>
        <p class="page-sub">
          {{ $t('集中管理文档，支持全文检索与 RAG 问答') }}
        </p>
      </div>
      <Button
        type="primary"
        @click="showCreateModal = true"
      >
        <template #icon>
          <PlusOutlined />
        </template>
        {{ $t('创建知识库') }}
      </Button>
    </div>

    <div class="list-toolbar">
      <Input
        v-model:value="searchQuery"
        :placeholder="$t('搜索知识库（名称 / 描述）')"
        allow-clear
        class="search-input"
      >
        <template #prefix>
          <SearchOutlined />
        </template>
      </Input>
    </div>

    <!-- 加载骨架（替代 Spin 空白） -->
    <PageSkeleton
      v-if="loading"
      variant="cards"
      :columns="3"
      :header="false"
    />

    <!-- 空状态：统一 EmptyState 组件，带引导 CTA -->
    <EmptyState
      v-else-if="knowledgeBases.length === 0"
      size="page"
      :icon="markRaw(BookOutlined)"
      :description="$t('暂无知识库')"
      :hint="$t('创建第一个知识库，开始文档检索与 RAG 问答')"
    >
      <Button
        type="primary"
        @click="showCreateModal = true"
      >
        <template #icon>
          <PlusOutlined />
        </template>
        {{ $t('创建知识库') }}
      </Button>
    </EmptyState>

    <div
      v-else
      class="kb-sections"
    >
      <div
        v-if="privateKbs.length > 0"
        class="kb-section"
      >
        <h2 class="section-title">
          {{ $t('我的知识库') }}
        </h2>
        <div class="kb-grid">
          <div
            v-for="kb in privateKbs"
            :key="kb.id"
            class="kb-card"
            role="button"
            tabindex="0"
            @click="openKnowledgeBase(kb)"
            @keydown.enter.prevent="openKnowledgeBase(kb)"
            @keydown.space.prevent="openKnowledgeBase(kb)"
          >
            <div class="card-top">
              <span class="card-icon"><BookOutlined /></span>
              <div class="card-titles">
                <span class="kb-name">{{ kb.name }}</span>
                <span class="kb-desc">{{ kb.description || $t('暂无描述') }}</span>
              </div>
              <Tag
                :color="kb.type === 'rag' ? 'success' : 'blue'"
                class="type-tag"
              >
                {{ kb.type.toUpperCase() }}
              </Tag>
              <Tag
                v-if="kb.visibility === 'tenant'"
                color="green"
                class="type-tag"
              >
                {{ $t('团队共享') }}
              </Tag>
            </div>
            <div class="kb-stats">
              <span class="stat"><FileTextOutlined /> {{ $t('{n} 文档', { n: kb.document_count }) }}</span>
              <span class="stat"><DatabaseOutlined /> {{ formatSize(kb.total_size_bytes) }}</span>
              <Tag :color="kb.status === 'active' ? 'green' : kb.status === 'building' ? 'processing' : 'default'">
                {{ kb.status }}
              </Tag>
              <Tag
                v-if="usageLabel(kb.id)"
                color="blue"
                :title="usageTitle(kb.id)"
              >
                {{ $t('被 {label} 使用', { label: usageLabel(kb.id) }) }}
              </Tag>
            </div>
            <div class="kb-footer">
              <span class="kb-time">{{ $t('更新于 {date}', { date: formatDate(kb.updated_at) }) }}</span>
              <div class="footer-actions">
                <Button
                  v-if="kb.visibility !== 'public'"
                  type="text"
                  size="small"
                  :title="kb.visibility === 'tenant' ? $t('设为私有') : $t('共享给团队')"
                  :loading="visibilityTogglingId === kb.id"
                  @click.stop="toggleVisibility(kb)"
                >
                  <template #icon>
                    <LockOutlined v-if="kb.visibility === 'tenant'" />
                    <TeamOutlined v-else />
                  </template>
                </Button>
                <Button
                  type="text"
                  size="small"
                  :title="$t('编辑')"
                  @click.stop="openEdit(kb)"
                >
                  <template #icon>
                    <EditOutlined />
                  </template>
                </Button>
                <Popconfirm
                  :title="$t('确认删除此知识库？')"
                  @confirm="deleteKnowledgeBase(kb.id)"
                >
                  <Button
                    type="text"
                    danger
                    size="small"
                    :title="$t('删除')"
                    @click.stop
                  >
                    <template #icon>
                      <DeleteOutlined />
                    </template>
                  </Button>
                </Popconfirm>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div
        v-if="publicKbs.length > 0"
        class="kb-section"
      >
        <h2 class="section-title">
          {{ $t('公共知识库') }}
        </h2>
        <div class="kb-grid">
          <div
            v-for="kb in publicKbs"
            :key="kb.id"
            class="kb-card public"
            role="button"
            tabindex="0"
            @click="openKnowledgeBase(kb)"
            @keydown.enter.prevent="openKnowledgeBase(kb)"
            @keydown.space.prevent="openKnowledgeBase(kb)"
          >
            <div class="card-top">
              <span class="card-icon"><BookOutlined /></span>
              <div class="card-titles">
                <span class="kb-name">{{ kb.name }}</span>
                <span class="kb-desc">{{ kb.description || $t('暂无描述') }}</span>
              </div>
              <Tag color="warning">
                {{ $t('公共') }}
              </Tag>
            </div>
            <div class="kb-stats">
              <span class="stat"><FileTextOutlined /> {{ $t('{n} 文档', { n: kb.document_count }) }}</span>
              <span class="stat"><DatabaseOutlined /> {{ formatSize(kb.total_size_bytes) }}</span>
              <Tag
                v-if="usageLabel(kb.id)"
                color="blue"
                :title="usageTitle(kb.id)"
              >
                {{ $t('被 {label} 使用', { label: usageLabel(kb.id) }) }}
              </Tag>
            </div>
            <div class="kb-footer">
              <span class="kb-time">{{ $t('更新于 {date}', { date: formatDate(kb.updated_at) }) }}</span>
              <div class="footer-actions">
                <Button
                  type="text"
                  size="small"
                  :title="$t('编辑')"
                  @click.stop="openEdit(kb)"
                >
                  <template #icon>
                    <EditOutlined />
                  </template>
                </Button>
                <Popconfirm
                  :title="$t('确认删除此知识库？')"
                  @confirm="deleteKnowledgeBase(kb.id)"
                >
                  <Button
                    type="text"
                    danger
                    size="small"
                    :title="$t('删除')"
                    @click.stop
                  >
                    <template #icon>
                      <DeleteOutlined />
                    </template>
                  </Button>
                </Popconfirm>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 创建 Modal -->
    <Modal
      :open="showCreateModal"
      :title="$t('创建知识库')"
      :confirm-loading="creating"
      :ok-text="$t('创建')"
      :cancel-text="$t('取消')"
      @ok="createKnowledgeBase"
      @cancel="showCreateModal = false"
    >
      <div class="editor-form">
        <div class="form-row">
          <label class="form-label">{{ $t('名称 *') }}</label>
          <Input
            v-model:value="createForm.name"
            :placeholder="$t('知识库名称')"
            :maxlength="60"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('描述') }}</label>
          <Input.TextArea
            v-model:value="createForm.description"
            :rows="2"
            :placeholder="$t('一句话描述内容范围')"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('类型') }}</label>
          <Radio.Group v-model:value="createForm.type">
            <Radio value="wiki">
              {{ $t('Wiki（全文检索）') }}
            </Radio>
            <Radio value="rag">
              {{ $t('RAG（向量问答）') }}
            </Radio>
          </Radio.Group>
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('可见性') }}</label>
          <Radio.Group v-model:value="createForm.visibility">
            <Radio value="private">
              {{ $t('私有') }}
            </Radio>
            <Radio value="public">
              {{ $t('公开') }}
            </Radio>
          </Radio.Group>
        </div>
      </div>
    </Modal>

    <!-- 编辑 Modal -->
    <Modal
      :open="showEditModal"
      :title="$t('编辑「{name}」', { name: editingKb?.name || '' })"
      :confirm-loading="saving"
      :ok-text="$t('保存')"
      :cancel-text="$t('取消')"
      @ok="saveEdit"
      @cancel="showEditModal = false"
    >
      <div class="editor-form">
        <div class="form-row">
          <label class="form-label">{{ $t('名称 *') }}</label>
          <Input
            v-model:value="editForm.name"
            :maxlength="60"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('描述') }}</label>
          <Input.TextArea
            v-model:value="editForm.description"
            :rows="2"
          />
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('类型') }}</label>
          <Radio.Group v-model:value="editForm.type">
            <Radio value="wiki">
              Wiki
            </Radio>
            <Radio value="rag">
              RAG
            </Radio>
          </Radio.Group>
        </div>
        <div class="form-row">
          <label class="form-label">{{ $t('可见性') }}</label>
          <Radio.Group v-model:value="editForm.visibility">
            <Radio value="private">
              {{ $t('私有') }}
            </Radio>
            <Radio value="public">
              {{ $t('公开') }}
            </Radio>
          </Radio.Group>
        </div>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.kb-page { max-width: 1080px; margin: 0 auto; padding: 28px 24px 60px; }
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.page-title { font-size: 24px; font-weight: 700; margin: 0; letter-spacing: -0.01em; }
.page-sub { margin: 4px 0 0; color: var(--text-tertiary); font-size: 13px; }
.list-toolbar { margin-bottom: 16px; }
.search-input { max-width: 320px; }
.page-empty { padding: 60px 0; }
.kb-sections { display: flex; flex-direction: column; gap: 24px; }
.section-title { font-size: 15px; font-weight: 600; color: var(--text-secondary); margin: 0 0 10px; }
.kb-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
.kb-card {
  display: flex; flex-direction: column; gap: 10px;
  padding: 16px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  cursor: pointer;
  transition: transform var(--dur-normal) ease, border-color var(--dur-normal) ease, box-shadow var(--dur-normal) ease;
}
.kb-card:hover { transform: translateY(-2px); border-color: var(--primary); box-shadow: var(--shadow-lg); }
.kb-card.public { border-left: 3px solid var(--warning); }
.card-top { display: flex; align-items: flex-start; gap: 10px; }
.card-icon { flex: none; width: 36px; height: 36px; border-radius: 10px; background: var(--primary-bg); color: var(--primary); display: inline-flex; align-items: center; justify-content: center; font-size: 17px; }
.card-titles { flex: 1; min-width: 0; }
.kb-name { display: block; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.kb-desc {
  display: block; margin-top: 3px; font-size: 12px; color: var(--text-tertiary);
  line-height: 1.5; overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}
.type-tag { flex: none; }
.kb-stats { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.stat { font-size: 12px; color: var(--text-secondary); display: inline-flex; align-items: center; gap: 4px; font-variant-numeric: tabular-nums; }
.stat :deep(svg) { font-size: 12px; color: var(--text-tertiary); }
.kb-footer { display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--border-card); padding-top: 10px; }
.kb-time { font-size: 11px; color: var(--text-tertiary); }
.footer-actions { display: flex; gap: 2px; }
.editor-form { display: flex; flex-direction: column; gap: 12px; }
.form-row { display: flex; flex-direction: column; gap: 6px; }
.form-label { font-size: 12px; color: var(--text-secondary); font-weight: 500; }
@media (max-width: 768px) {
  .kb-page { padding: 20px 16px 48px; }
  .search-input { max-width: none; width: 100%; }
}

@media (max-width: 640px) {
  .kb-grid { grid-template-columns: 1fr; }
  .page-head { flex-direction: column; align-items: flex-start; }
  .page-head > .ant-btn { width: 100%; }
}
</style>
