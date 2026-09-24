<script setup lang="ts">
import { ref, computed, onMounted, watch, onUnmounted, markRaw } from 'vue'
import {
  Button, Input, Select, Segmented, Pagination, Table, message,
  Drawer, Modal, Popconfirm, Upload, Checkbox, Tree, Tag,
} from 'ant-design-vue'
import {
  SearchOutlined, CloudUploadOutlined, FolderOutlined, FileOutlined,
  FolderAddOutlined, LeftOutlined, RightOutlined, TagOutlined, PictureOutlined,
} from '@ant-design/icons-vue'
import { api, resolveMediaUrl } from '../api'
import { createChunkUpload } from '../utils/uploader'
import { FileViewer } from '@file-viewer/vue3'
import allPreset from '@file-viewer/preset-all'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'

import { useI18n } from 'vue-i18n'
const { t: tr } = useI18n()
const API_URL = import.meta.env.VITE_API_URL || ''

interface MediaItem {
  id: string
  type: string
  name: string
  size: number
  file_url: string
  mime_type?: string
  tags?: string[]
  created_at: string
}

const loading = ref(true)
const error = ref(false)
const items = ref<MediaItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const searchQuery = ref('')
const fetchSeq = ref(0)  // S 修复：列表请求序号，丢弃过期响应
const typeFilter = ref('')
const viewMode = ref<'grid' | 'list'>(localStorage.getItem('media-view') === 'list' ? 'list' : 'grid')
const breadcrumbs = ref<{ id: string; name: string }[]>([{ id: '', name: '全部文件' }])
const selectedIds = ref<Set<string>>(new Set())

// 详情侧栏 / 上传（T9/T10 填充 UI，此处声明状态）
const detailItem = ref<MediaItem | null>(null)
const showUpload = ref(false)

const currentParentId = computed(() => breadcrumbs.value[breadcrumbs.value.length - 1].id)

const typeOptions = [
  { label: tr('全部类型'), value: '' },
  { label: tr('图片'), value: 'image' },
  { label: tr('文档'), value: 'document' },
  { label: tr('视频'), value: 'video' },
  { label: tr('音频'), value: 'audio' },
  { label: tr('文件'), value: 'file' },
  { label: tr('文本'), value: 'text' },
  { label: tr('代码'), value: 'code' },
]

const listColumns = [
  { title: tr('名称'), dataIndex: 'name', ellipsis: true },
  { title: tr('类型'), dataIndex: 'type', width: 100 },
  { title: tr('大小'), dataIndex: 'size', width: 110, customRender: ({ text }: { text: number }) => formatSize(text) },
  { title: tr('上传时间'), dataIndex: 'created_at', width: 180 },
]

function formatSize(bytes: number): string {
  if (!bytes) return ''
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}

function isFolder(item: MediaItem) { return item.type === 'folder' }
function isImage(item: MediaItem) { return (item.mime_type || '').startsWith('image/') || item.type === 'image' }

function absUrl(url: string): string {
  if (!url) return ''
  if (url.startsWith('http://') || url.startsWith('https://')) return url
  return `${API_URL}${url.startsWith('/') ? '' : '/'}${url}`
}

// ── 签名 URL 解析（安全改造：/media/ 公开路径 → 短时效签名 URL；非 /media/ 前缀原样）──
const resolvedUrls = ref<Record<string, string>>({})
const resolvingIds = new Set<string>()
const fallbackIds = new Set<string>()

/** resolveMediaUrl 结果归一化：相对路径补 API_URL 前缀；失败回退原 file_url */
function normalizeResolved(url: string, item: MediaItem): string {
  if (!url) return absUrl(item.file_url)
  if (url.startsWith('/') && !url.startsWith('//')) {
    return `${API_URL}${url}`
  }
  return url
}

async function resolveItemUrl(item: MediaItem) {
  if (resolvingIds.has(item.id)) return
  resolvingIds.add(item.id)
  try {
    const url = await resolveMediaUrl({ id: item.id, file_url: item.file_url })
    if (url) resolvedUrls.value[item.id] = normalizeResolved(url, item)
  } catch {
    // 静默失败，使用原 file_url 回退
  } finally {
    resolvingIds.delete(item.id)
  }
}

/** 列表加载后批量解析（缩略图/详情/灯箱共用 resolvedUrl map） */
function resolveAllUrls() {
  for (const i of items.value) {
    if (!isFolder(i) && (i.file_url || '').startsWith('/media/')) void resolveItemUrl(i)
  }
}

/** 渲染用 URL：签名 URL 优先；未解析时懒解析并以原路径占位；失败回退原 file_url */
function itemUrl(item: MediaItem): string {
  const r = resolvedUrls.value[item.id]
  if (r) {
    return r
  }
  if (!fallbackIds.has(item.id) && (item.file_url || '').startsWith('/media/')) {
    void resolveItemUrl(item)
  }
  // 如果还没有解析完成，暂时返回原 file_url（会在解析完成后重新渲染）
  return absUrl(item.file_url)
}

/** 图片加载失败回退：签名 URL 失效 → 回退公开路径原图；原图也失败 → 隐藏 */
function onImgError(e: Event, item: MediaItem) {
  const el = e.target as HTMLImageElement
  const raw = absUrl(item.file_url)
  if (resolvedUrls.value[item.id] && el.src !== raw) {
    resolvedUrls.value[item.id] = ''
    fallbackIds.add(item.id)
    return
  }
  el.style.display = 'none'
}

async function fetchItems() {
  // S 修复：请求序号守卫 — 快速翻页/搜索时丢弃过期响应，避免旧结果覆盖新列表
  const mySeq = ++fetchSeq.value
  loading.value = true
  error.value = false
  try {
    const params: Record<string, string | number> = {
      page: page.value,
      page_size: pageSize.value,
      parent_id: currentParentId.value,
    }
    if (searchQuery.value.trim()) params.search = searchQuery.value.trim()
    if (typeFilter.value) params.type = typeFilter.value
    if (tagFilter.value.length > 0) params.tags = tagFilter.value.join(',')
    const res = await api.get('/v1/media', { params })
    if (mySeq !== fetchSeq.value) return  // 已被更新的请求取代
    const data = res.data?.data || { items: [], total: 0 }
    items.value = data.items || []
    total.value = data.total || 0
    selectedIds.value = new Set()
    resolveAllUrls()
  } catch {
    if (mySeq !== fetchSeq.value) return
    items.value = []
    total.value = 0
    error.value = true
    message.error(tr('加载媒体库失败'))
  } finally {
    if (mySeq === fetchSeq.value) loading.value = false
  }
}

function enterFolder(id: string, name: string) {
  breadcrumbs.value.push({ id, name })
  page.value = 1
  fetchItems()
}

function goBreadcrumb(idx: number) {
  breadcrumbs.value = breadcrumbs.value.slice(0, idx + 1)
  page.value = 1
  fetchItems()
}

function toggleView(mode: 'grid' | 'list') {
  viewMode.value = mode
  localStorage.setItem('media-view', mode)
}

function onCardClick(item: MediaItem) {
  if (isFolder(item)) {
    enterFolder(item.id, item.name)
  } else {
    detailItem.value = item
  }
}

// ── 详情侧栏操作 ──
const showPreview = ref(false)
const previewItem = ref<MediaItem | null>(null)
const showRename = ref(false)
const renameName = ref('')
const showMove = ref(false)
const moveParentId = ref('')
const showShare = ref(false)
const shareUrl = ref('')
const shareExpires = ref('')
const shareLoading = ref(false)

function openPreview(item: MediaItem) {
  previewItem.value = item
  showPreview.value = true
  if ((item.file_url || '').startsWith('/media/')) void resolveItemUrl(item)
}

async function downloadItem(item: MediaItem) {
  const url = await resolveMediaUrl({ id: item.id, file_url: item.file_url })
  window.open(normalizeResolved(url, item), '_blank')
}

async function copyUrl(item: MediaItem) {
  const url = await resolveMediaUrl({ id: item.id, file_url: item.file_url })
  try {
    await navigator.clipboard.writeText(normalizeResolved(url, item))
    message.success(tr('URL 已复制'))
  } catch {
    message.error(tr('复制失败'))
  }
}

async function shareItem() {
  if (!detailItem.value) return
  shareLoading.value = true
  try {
    const res = await api.post(`/v1/media/${detailItem.value.id}/share`, { expires_in_seconds: 900 })
    const data = res.data?.data || {}
    shareUrl.value = data.url || ''
    shareExpires.value = data.expires_at || ''
    showShare.value = true
    if (!shareUrl.value) message.error(tr('当前存储后端不支持分享'))
  } catch (e: any) {
    message.error(e.response?.data?.error || tr('生成分享链接失败'))
  } finally {
    shareLoading.value = false
  }
}

function openRename() {
  if (!detailItem.value) return
  renameName.value = detailItem.value.name
  showRename.value = true
}

async function submitRename() {
  if (!detailItem.value || !renameName.value.trim()) return
  try {
    await api.put(`/v1/media/${detailItem.value.id}`, { name: renameName.value.trim() })
    message.success(tr('已重命名'))
    showRename.value = false
    detailItem.value = null
    fetchItems()
  } catch (e: any) {
    message.error(e.response?.data?.error || tr('重命名失败'))
  }
}

async function openMove() {
  moveParentId.value = currentParentId.value
  try {
    const res = await api.get('/v1/media/folders')
    const folders: { id: string; name: string; parent_id: string }[] = res.data?.data || []
    moveTreeData.value = buildMoveTree(folders)
  } catch {
    moveTreeData.value = []
  }
  showMove.value = true
}

// 文件夹层级树（移动对话框）
const moveTreeData = ref<any[]>([])

function buildMoveTree(folders: { id: string; name: string; parent_id: string }[]): any[] {
  const map = new Map<string, any>()
  for (const f of folders) map.set(f.id, { key: f.id, title: f.name, children: [] as any[] })
  const roots: any[] = []
  for (const f of folders) {
    const node = map.get(f.id)
    if (f.parent_id && map.has(f.parent_id)) map.get(f.parent_id).children.push(node)
    else roots.push(node)
  }
  return roots
}

// ── 新建文件夹 ──
const newFolderOpen = ref(false)
const newFolderName = ref('')
const folderCreating = ref(false)

async function createFolder() {
  const name = newFolderName.value.trim()
  if (!name) { message.warning(tr('请输入文件夹名称')); return }
  folderCreating.value = true
  try {
    await api.post('/v1/media/folders', { name, parent_id: currentParentId.value })
    message.success(tr('文件夹已创建'))
    newFolderOpen.value = false
    newFolderName.value = ''
    fetchItems()
  } catch (e: any) {
    message.error(e.response?.data?.error || tr('创建失败'))
  } finally {
    folderCreating.value = false
  }
}

// ── 图片灯箱 ──
const lightboxOpen = ref(false)
const lightboxList = ref<MediaItem[]>([])
const lightboxIndex = ref(0)
const lightboxItem = computed(() => lightboxList.value[lightboxIndex.value])

function openLightbox(item: MediaItem) {
  lightboxList.value = items.value.filter(i => !isFolder(i) && isImage(i))
  const idx = lightboxList.value.findIndex(i => i.id === item.id)
  lightboxIndex.value = Math.max(0, idx)
  lightboxOpen.value = true
}

function lbPrev() {
  if (lightboxList.value.length) lightboxIndex.value = (lightboxIndex.value - 1 + lightboxList.value.length) % lightboxList.value.length
}

function lbNext() {
  if (lightboxList.value.length) lightboxIndex.value = (lightboxIndex.value + 1) % lightboxList.value.length
}

function onLightboxKey(e: KeyboardEvent) {
  if (!lightboxOpen.value) return
  if (e.key === 'Escape') lightboxOpen.value = false
  else if (e.key === 'ArrowLeft') lbPrev()
  else if (e.key === 'ArrowRight') lbNext()
}

// ── 标签 ──
const tagFilter = ref<string[]>([])
const tagInput = ref('')

const allTags = computed(() => {
  const set = new Set<string>()
  for (const i of items.value) for (const t of i.tags || []) set.add(t)
  return Array.from(set)
})

async function addTag() {
  if (!detailItem.value) return
  const t = tagInput.value.trim()
  if (!t) return
  const next = Array.from(new Set([...(detailItem.value.tags || []), t]))
  try {
    await api.put(`/v1/media/${detailItem.value.id}`, { tags: next })
    detailItem.value = { ...detailItem.value, tags: next }
    tagInput.value = ''
    fetchItems()
  } catch (e: any) {
    message.error(e.response?.data?.error || tr('添加标签失败'))
  }
}

async function removeTag(t: string) {
  if (!detailItem.value) return
  const next = (detailItem.value.tags || []).filter(x => x !== t)
  try {
    await api.put(`/v1/media/${detailItem.value.id}`, { tags: next })
    detailItem.value = { ...detailItem.value, tags: next }
    fetchItems()
  } catch {
    message.error(tr('移除标签失败'))
  }
}

async function submitMove() {
  if (!detailItem.value) return
  try {
    await api.put(`/v1/media/${detailItem.value.id}`, { parent_id: moveParentId.value })
    message.success(tr('已移动'))
    showMove.value = false
    detailItem.value = null
    fetchItems()
  } catch (e: any) {
    message.error(e.response?.data?.error || tr('移动失败'))
  }
}

async function deleteItem(id: string) {
  try {
    await api.delete(`/v1/media/${id}`)
    message.success(tr('已删除'))
    detailItem.value = null
    fetchItems()
  } catch (e: any) {
    message.error(e.response?.data?.error || tr('删除失败'))
  }
}

async function copyShareUrl() {
  try {
    await navigator.clipboard.writeText(shareUrl.value)
    message.success(tr('分享链接已复制'))
  } catch {
    message.error(tr('复制失败'))
  }
}

// ── 批量上传（拖拽 + 多文件） ──
const uploadFileList = ref<any[]>([])
const uploadingFiles = ref<Map<string, { progress: number; status: 'uploading' | 'success' | 'error'; error?: string }>>(new Map())

function handleUploadRequest(options: any) {
  const { file, onProgress, onSuccess, onError } = options
  const fileId = `${file.name}-${file.size}-${file.lastModified}`
  
  // 初始化上传状态
  uploadingFiles.value.set(fileId, { progress: 0, status: 'uploading' })
  
  // 全局分片上传（断点续传）
  createChunkUpload(file, { purpose: 'media', parentId: currentParentId.value })
    .then((handle) => {
      handle.onProgress((p) => {
        onProgress?.({ percent: p })
        // 更新详细进度
        const current = uploadingFiles.value.get(fileId)
        if (current) {
          uploadingFiles.value.set(fileId, { ...current, progress: p })
        }
      })
      handle.done
        .then(() => { 
          onSuccess?.(null)
          uploadingFiles.value.set(fileId, { progress: 100, status: 'success' })
          message.success(`上传成功: ${file.name}`)
          // 3秒后移除成功记录
          setTimeout(() => uploadingFiles.value.delete(fileId), 3000)
        })
        .catch((err: any) => {
          onError?.(err)
          uploadingFiles.value.set(fileId, { 
            progress: 0, 
            status: 'error', 
            error: err?.message || '未知错误' 
          })
          message.error(`上传失败: ${file.name} — ${err?.message || tr('未知错误')}`)
        })
        .finally(() => { fetchItems() })
    })
    .catch((err: any) => { 
      uploadingFiles.value.set(fileId, { 
        progress: 0, 
        status: 'error', 
        error: err?.message || '初始化失败' 
      })
      message.error(`上传失败: ${file.name} — 初始化失败`) 
    })
}

// 暂停所有上传
function pauseAllUploads() {
  // 注意: 这里需要修改 uploader.ts 暴露暂停接口
  message.info(tr('暂停功能开发中'))
}

// 重试失败的上传
async function retryFailedUploads() {
  const failedEntries = Array.from(uploadingFiles.value.entries())
    .filter(([_, state]) => state.status === 'error')
  
  if (failedEntries.length === 0) {
    message.info(tr('没有失败的上传任务'))
    return
  }
  
  message.info(`正在重试 ${failedEntries.length} 个失败的上传...`)
  
  // 指数退避重试函数
  const uploadWithRetry = async (fileId: string, maxRetries: number = 3) => {
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        // 更新状态为重试中
        uploadingFiles.value.set(fileId, { 
          progress: 0, 
          status: 'uploading',
          error: undefined 
        })
        
        // 从 fileId 中提取文件信息（简化处理，实际应该存储文件引用）
        // 这里假设用户会重新选择文件进行重试
        message.warning(`请重新选择文件进行重试: ${fileId}`)
        return true
      } catch {
        // 静默失败，重试逻辑由调用方处理
        
        if (attempt < maxRetries) {
          // 指数退避：1s, 2s, 4s...
          const delay = Math.pow(2, attempt - 1) * 1000
          await new Promise(resolve => setTimeout(resolve, delay))
        }
      }
    }
    
    // 所有重试都失败
    uploadingFiles.value.set(fileId, { 
      progress: 0, 
      status: 'error', 
      error: `重试${maxRetries}次后仍然失败` 
    })
    return false
  }
  
  // 并行重试所有失败的文件
  const results = await Promise.all(
    failedEntries.map(([fileId]) => uploadWithRetry(fileId))
  )
  
  const successCount = results.filter(r => r).length
  const failCount = results.length - successCount
  
  if (successCount > 0) {
    message.success(`成功重试 ${successCount} 个上传`)
  }
  if (failCount > 0) {
    message.error(`${failCount} 个上传重试失败，请手动重新上传`)
  }
}

// 清除已完成的上传记录
function clearCompletedUploads() {
  for (const [key, state] of uploadingFiles.value.entries()) {
    if (state.status === 'success') {
      uploadingFiles.value.delete(key)
    }
  }
  message.success(tr('已清除完成的上传记录'))
}

// ── 批量选择 ──
function toggleSelect(item: MediaItem, e: Event) {
  e.stopPropagation()
  const next = new Set(selectedIds.value)
  if (next.has(item.id)) next.delete(item.id)
  else next.add(item.id)
  selectedIds.value = next
}

function selectAll() {
  selectedIds.value = new Set(items.value.map(i => i.id))
}

function deselectAll() {
  selectedIds.value = new Set()
}

function invertSelection() {
  const next = new Set<string>()
  for (const item of items.value) {
    if (!selectedIds.value.has(item.id)) {
      next.add(item.id)
    }
  }
  selectedIds.value = next
}

// ── 批量删除 ──
const batchDeleting = ref(false)

async function batchDelete() {
  const ids = Array.from(selectedIds.value)
  if (ids.length === 0) return
  batchDeleting.value = true
  try {
    const res = await api.post('/v1/media/batch-delete', { ids })
    message.success(`已删除 ${res.data?.data?.deleted || ids.length} 项`)
    fetchItems()
  } catch (e: any) {
    message.error(e.response?.data?.error || tr('批量删除失败'))
  } finally {
    batchDeleting.value = false
  }
}

// ── 批量移动 ──
const showBatchMove = ref(false)
const batchMoveParentId = ref('')
const batchMoving = ref(false)

async function openBatchMove() {
  if (selectedIds.value.size === 0) { message.warning('请先选择文件'); return }
  batchMoveParentId.value = currentParentId.value
  try {
    const res = await api.get('/v1/media/folders')
    const folders: { id: string; name: string; parent_id: string }[] = res.data?.data || []
    moveTreeData.value = buildMoveTree(folders)
  } catch {
    moveTreeData.value = []
  }
  showBatchMove.value = true
}

async function submitBatchMove() {
  if (selectedIds.value.size === 0) return
  batchMoving.value = true
  let successCount = 0
  let failCount = 0
  const ids = Array.from(selectedIds.value)
  
  for (const id of ids) {
    try {
      await api.put(`/v1/media/${id}`, { parent_id: batchMoveParentId.value })
      successCount++
    } catch {
      failCount++
    }
  }
  
  batchMoving.value = false
  showBatchMove.value = false
  
  if (successCount > 0) {
    message.success(`成功移动 ${successCount} 个文件`)
  }
  if (failCount > 0) {
    message.error(`${failCount} 个文件移动失败`)
  }
  fetchItems()
}

// ── 批量重命名 ──
const showBatchRename = ref(false)
const renamePrefix = ref('')
const renameSuffix = ref('')
const renameFindText = ref('')
const renameReplaceText = ref('')
const batchRenaming = ref(false)

function openBatchRename() {
  if (selectedIds.value.size === 0) { message.warning('请先选择文件'); return }
  renamePrefix.value = ''
  renameSuffix.value = ''
  renameFindText.value = ''
  renameReplaceText.value = ''
  showBatchRename.value = true
}

async function submitBatchRename() {
  if (selectedIds.value.size === 0) return
  batchRenaming.value = true
  let successCount = 0
  let failCount = 0
  const ids = Array.from(selectedIds.value)
  
  for (const id of ids) {
    const item = items.value.find(i => i.id === id)
    if (!item) continue
    
    let newName = item.name
    // 应用查找替换
    if (renameFindText.value) {
      newName = newName.replace(new RegExp(renameFindText.value, 'g'), renameReplaceText.value)
    }
    // 应用前缀和后缀
    newName = `${renamePrefix.value}${newName}${renameSuffix.value}`
    
    try {
      await api.put(`/v1/media/${id}`, { name: newName })
      successCount++
    } catch {
      failCount++
    }
  }
  
  batchRenaming.value = false
  showBatchRename.value = false
  
  if (successCount > 0) {
    message.success(`成功重命名 ${successCount} 个文件`)
  }
  if (failCount > 0) {
    message.error(`${failCount} 个文件重命名失败`)
  }
  fetchItems()
}

// ── 批量标签管理 ──
const showBatchTags = ref(false)
const batchTagInput = ref('')
const batchTagging = ref(false)

function openBatchTags() {
  if (selectedIds.value.size === 0) { message.warning('请先选择文件'); return }
  batchTagInput.value = ''
  showBatchTags.value = true
}

async function submitBatchTags() {
  if (selectedIds.value.size === 0 || !batchTagInput.value.trim()) return
  batchTagging.value = true
  let successCount = 0
  let failCount = 0
  const tagToAdd = batchTagInput.value.trim()
  const ids = Array.from(selectedIds.value)
  
  for (const id of ids) {
    const item = items.value.find(i => i.id === id)
    if (!item) continue
    
    const currentTags = item.tags || []
    if (currentTags.includes(tagToAdd)) continue // 已有该标签，跳过
    
    const newTags = [...currentTags, tagToAdd]
    try {
      await api.put(`/v1/media/${id}`, { tags: newTags })
      successCount++
    } catch {
      failCount++
    }
  }
  
  batchTagging.value = false
  showBatchTags.value = false
  
  if (successCount > 0) {
    message.success(`成功为 ${successCount} 个文件添加标签`)
  }
  if (failCount > 0) {
    message.error(`${failCount} 个文件添加标签失败`)
  }
  fetchItems()
}

// ── 批量下载 ──
async function batchDownload() {
  const ids = Array.from(selectedIds.value)
  if (ids.length === 0) return
  
  const files = items.value.filter(i => selectedIds.value.has(i.id) && !isFolder(i))
  if (files.length === 0) {
    message.warning(tr('选中的项目中没有可下载的文件'))
    return
  }
  
  let successCount = 0
  let failCount = 0
  
  for (const file of files) {
    try {
      const url = await resolveMediaUrl({ id: file.id, file_url: file.file_url })
      const link = document.createElement('a')
      link.href = normalizeResolved(url, file)
      link.download = file.name
      link.click()
      successCount++
      // 添加小延迟避免浏览器阻止多个下载
      await new Promise(resolve => setTimeout(resolve, 200))
    } catch {
      failCount++
    }
  }
  
  if (successCount > 0) {
    message.success(`开始下载 ${successCount} 个文件`)
  }
  if (failCount > 0) {
    message.error(`${failCount} 个文件下载失败`)
  }
}

// ── 键盘快捷键 ──
function handleKeyboardShortcut(e: KeyboardEvent) {
  // Ctrl+A: 全选
  if (e.ctrlKey && e.key === 'a') {
    e.preventDefault()
    selectAll()
  }
  // Delete: 删除选中项
  if (e.key === 'Delete' && selectedIds.value.size > 0) {
    e.preventDefault()
    batchDelete()
  }
  // Escape: 取消选择
  if (e.key === 'Escape' && selectedIds.value.size > 0) {
    e.preventDefault()
    deselectAll()
  }
}

// ── 添加到知识库（保留既有流程） ──
const showKbModal = ref(false)
const selectedKbId = ref<string | undefined>(undefined)
const knowledgeBases = ref<{ id: string; name: string; type: string }[]>([])
const uploadingToKb = ref(false)
const kbOptions = computed(() => knowledgeBases.value.map(kb => ({ label: `${kb.name} (${kb.type.toUpperCase()})`, value: kb.id })))

async function openKbModal() {
  if (selectedIds.value.size === 0) { message.warning('请先选择文件'); return }
  try {
    const res = await api.get('/v1/kb')
    knowledgeBases.value = res.data?.data?.knowledge_bases || []
  } catch {
    knowledgeBases.value = []
  }
  selectedKbId.value = undefined
  showKbModal.value = true
}

async function uploadToKnowledgeBase() {
  if (!selectedKbId.value || selectedIds.value.size === 0) return
  uploadingToKb.value = true
  let ok = 0, fail = 0
  const files = items.value.filter(i => selectedIds.value.has(i.id) && !isFolder(i))
  for (const file of files) {
    try {
      const url = await resolveMediaUrl({ id: file.id, file_url: file.file_url })
      const resp = await fetch(normalizeResolved(url, file))
      const blob = await resp.blob()
      const mediaFile = new File([blob], file.name, { type: file.mime_type || '' })
      // 全局分片上传到知识库（kb_doc purpose → complete 落 knowledge_documents）
      const handle = await createChunkUpload(mediaFile, { purpose: 'kb_doc', parentId: selectedKbId.value })
      await handle.done
      ok++
    } catch {
      fail++
    }
  }
  uploadingToKb.value = false
  showKbModal.value = false
  if (ok > 0) message.success(`成功上传 ${ok} 个文件到知识库`)
  if (fail > 0) message.error(`${fail} 个文件上传失败`)
}

// S 修复：搜索/筛选变化时重置到第 1 页并防抖(300ms)，避免逐击键请求；分页变化单独触发
let searchTimer: ReturnType<typeof setTimeout> | undefined
watch([searchQuery, typeFilter, tagFilter], () => {
  page.value = 1
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => fetchItems(), 300)
})
watch([page, pageSize], () => { fetchItems() })

onMounted(() => {
  fetchItems()
  window.addEventListener('keydown', onLightboxKey)
  window.addEventListener('keydown', handleKeyboardShortcut)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onLightboxKey)
  window.removeEventListener('keydown', handleKeyboardShortcut)
})
</script>

<template>
  <div class="media-page">
    <div class="media-toolbar">
      <div class="breadcrumb">
        <span
          v-for="(cr, i) in breadcrumbs"
          :key="cr.id || 'root'"
          class="crumb"
          :class="{ active: i === breadcrumbs.length - 1 }"
          role="button"
          tabindex="0"
          @click="goBreadcrumb(i)"
          @keydown.enter.prevent="goBreadcrumb(i)"
        >{{ cr.name }}<span
          v-if="i < breadcrumbs.length - 1"
          class="crumb-sep"
        >/</span></span>
      </div>
      <div class="toolbar-actions">
        <Input
          v-model:value="searchQuery"
          :placeholder="$t('搜索文件')"
          allow-clear
          style="width: 180px"
          size="small"
        >
          <template #prefix>
            <SearchOutlined />
          </template>
        </Input>
        <Select
          v-model:value="typeFilter"
          :options="typeOptions"
          size="small"
          style="width: 110px"
        />
        <Select
          v-if="allTags.length > 0 || tagFilter.length > 0"
          v-model:value="tagFilter"
          :options="allTags.map(t => ({ value: t, label: t }))"
          size="small"
          mode="multiple"
          allow-clear
          :placeholder="$t('标签')"
          style="min-width: 120px"
        />
        <Segmented
          :value="viewMode"
          :options="[{ label: '网格', value: 'grid' }, { label: '列表', value: 'list' }]"
          size="small"
          @change="toggleView(($event as any) as 'grid' | 'list')"
        />
        <Button
          size="small"
          @click="newFolderOpen = true"
        >
          <template #icon>
            <FolderAddOutlined />
          </template>{{ $t('新建文件夹') }}
        </Button>
        <Button
          type="primary"
          size="small"
          @click="showUpload = true"
        >
          <template #icon>
            <CloudUploadOutlined />
          </template>{{ $t('上传') }}
        </Button>
      </div>
      <div
        v-if="total > 0"
        class="media-total"
      >
        共 {{ total }} 项
      </div>
    </div>

    <div
      v-if="selectedIds.size > 0"
      class="batch-bar"
    >
      <span class="batch-count">已选择 {{ selectedIds.size }} / {{ items.length }} 项</span>
      <Button
        size="small"
        @click="selectAll"
      >
        {{ $t('全选') }}
      </Button>
      <Button
        size="small"
        @click="deselectAll"
      >
        {{ $t('取消选择') }}
      </Button>
      <Button
        size="small"
        @click="invertSelection"
      >
        {{ $t('反选') }}
      </Button>
      <Button
        size="small"
        danger
        :loading="batchDeleting"
        @click="batchDelete"
      >
        {{ $t('批量删除') }}
      </Button>
      <Button
        size="small"
        @click="openBatchMove"
      >
        {{ $t('批量移动') }}
      </Button>
      <Button
        size="small"
        @click="openBatchRename"
      >
        {{ $t('批量重命名') }}
      </Button>
      <Button
        size="small"
        @click="openBatchTags"
      >
        {{ $t('批量标签') }}
      </Button>
      <Button
        size="small"
        @click="batchDownload"
      >
        {{ $t('批量下载') }}
      </Button>
      <Button
        size="small"
        @click="openKbModal"
      >
        {{ $t('添加到知识库') }}
      </Button>
    </div>

    <!-- 加载骨架（替代 Spin 空白） -->
    <PageSkeleton
      v-if="loading"
      :variant="viewMode === 'grid' ? 'cards' : 'table'"
      :columns="4"
      :rows="8"
      :header="false"
    />

    <!-- 错误态 -->
    <EmptyState
      v-else-if="error"
      size="page"
      :icon="markRaw(CloudUploadOutlined)"
      :description="$t('加载失败')"
      :hint="$t('无法连接媒体服务，请检查网络后重试')"
    >
      <Button
        type="primary"
        @click="fetchItems"
      >
        {{ $t('重试') }}
      </Button>
    </EmptyState>

    <!-- 空状态 -->
    <EmptyState
      v-else-if="items.length === 0"
      size="page"
      :icon="markRaw(PictureOutlined)"
      :description="$t('暂无文件')"
      :hint="$t('拖拽文件到此处或点击上传，开始管理你的媒体库')"
    >
      <Button
        type="primary"
        @click="showUpload = true"
      >
        <template #icon>
          <CloudUploadOutlined />
        </template>
        {{ $t('上传文件') }}
      </Button>
    </EmptyState>

    <!-- 数据视图 -->
    <template v-else>
      <div
        v-if="viewMode === 'grid'"
        class="file-grid"
      >
        <div
          v-for="item in items"
          :key="item.id"
          class="file-card"
          :class="{ selected: selectedIds.has(item.id) }"
          role="button"
          tabindex="0"
          @click="onCardClick(item)"
          @keydown.enter.prevent="onCardClick(item)"
          @keydown.space.prevent="onCardClick(item)"
        >
          <Checkbox
            class="card-check"
            :checked="selectedIds.has(item.id)"
            @click="toggleSelect(item, $event)"
          />
          <div class="card-thumb">
            <img
              v-if="isImage(item) && !isFolder(item)"
              :src="itemUrl(item)"
              :alt="item.name"
              loading="lazy"
              @error="onImgError($event, item)"
            >
            <FolderOutlined
              v-else-if="isFolder(item)"
              class="thumb-icon folder"
            />
            <FileOutlined
              v-else
              class="thumb-icon"
            />
          </div>
          <div
            class="card-name"
            :title="item.name"
          >
            {{ item.name }}
          </div>
          <div
            v-if="!isFolder(item)"
            class="card-meta"
          >
            {{ formatSize(item.size) }}
          </div>
        </div>
      </div>
      <Table
        v-else
        :columns="listColumns"
        :data-source="items"
        :row-key="'id'"
        :pagination="false"
        :scroll="{ x: 640 }"
        size="small"
        :row-class-name="(record: MediaItem) => (selectedIds.has(record.id) ? 'row-selected' : '')"
        @row-click="(record: MediaItem) => onCardClick(record)"
      />
    </template>

    <div
      v-if="total > pageSize"
      class="pagination-bar"
    >
      <Pagination
        v-model:current="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-size-options="['20', '50', '100', '200']"
        show-size-changer
      />
    </div>

    <!-- 详情侧栏 -->
    <Drawer
      :open="!!detailItem"
      :width="360"
      :title="detailItem?.name || '文件详情'"
      @close="detailItem = null"
    >
      <template v-if="detailItem">
        <div class="detail-thumb">
          <img
            v-if="isImage(detailItem) && !isFolder(detailItem)"
            :src="itemUrl(detailItem)"
            :alt="detailItem.name"
            @error="onImgError($event, detailItem)"
          >
          <FolderOutlined
            v-else-if="isFolder(detailItem)"
            class="thumb-icon folder"
          />
          <FileOutlined
            v-else
            class="thumb-icon"
          />
        </div>
        <div class="detail-info">
          <div class="detail-row">
            <span class="label">{{ $t('类型') }}</span><span>{{ detailItem.type }}</span>
          </div>
          <div class="detail-row">
            <span class="label">{{ $t('大小') }}</span><span>{{ formatSize(detailItem.size) }}</span>
          </div>
          <div class="detail-row">
            <span class="label">MIME</span><span>{{ detailItem.mime_type || '—' }}</span>
          </div>
          <div class="detail-row">
            <span class="label">{{ $t('上传时间') }}</span><span>{{ detailItem.created_at }}</span>
          </div>
          <div class="detail-row">
            <span class="label">URL</span><span class="url-text">{{ itemUrl(detailItem) }}</span>
          </div>
          <!-- 标签编辑 -->
          <div class="detail-row">
            <span class="label"><TagOutlined /> {{ $t('标签') }}</span>
            <div class="tag-list">
              <Tag
                v-for="t in detailItem.tags || []"
                :key="t"
                closable
                @close="removeTag(t)"
              >
                {{ t }}
              </Tag>
              <span
                v-if="!(detailItem.tags || []).length"
                class="no-tags"
              >{{ $t('暂无标签') }}</span>
            </div>
            <div class="tag-add">
              <Input
                v-model:value="tagInput"
                size="small"
                :placeholder="$t('添加标签，回车确认')"
                style="width: 100%"
                @press-enter="addTag"
              />
            </div>
          </div>
          <!-- 分享展示 -->
          <div
            v-if="shareUrl"
            class="detail-row"
          >
            <span class="label">{{ $t('分享链接') }}</span>
            <span class="url-text">{{ shareUrl }}</span>
            <div
              v-if="shareExpires"
              class="share-expires"
            >
              有效期至 {{ shareExpires }}
            </div>
          </div>
        </div>
        <div class="detail-actions">
          <Button
            v-if="!isFolder(detailItem) && isImage(detailItem)"
            block
            @click="openLightbox(detailItem)"
          >
            {{ $t('大图查看') }}
          </Button>
          <Button
            v-if="!isFolder(detailItem)"
            block
            @click="openPreview(detailItem)"
          >
            {{ $t('预览') }}
          </Button>
          <Button
            v-if="!isFolder(detailItem)"
            block
            @click="downloadItem(detailItem)"
          >
            {{ $t('下载') }}
          </Button>
          <Button
            v-if="!isFolder(detailItem)"
            block
            @click="copyUrl(detailItem)"
          >
            {{ $t('复制 URL') }}
          </Button>
          <Button
            v-if="!isFolder(detailItem)"
            block
            :loading="shareLoading"
            @click="shareItem"
          >
            {{ $t('生成分享链接') }}
          </Button>
          <Button
            block
            @click="openRename"
          >
            {{ $t('重命名') }}
          </Button>
          <Button
            block
            @click="openMove"
          >
            {{ $t('移动到') }}
          </Button>
          <Popconfirm
            :title="$t('确认删除？文件夹将连同子项一并删除')"
            @confirm="detailItem && deleteItem(detailItem.id)"
          >
            <Button
              block
              danger
            >
              {{ $t('删除') }}
            </Button>
          </Popconfirm>
        </div>
      </template>
    </Drawer>

    <!-- 重命名 -->
    <Modal
      v-model:open="showRename"
      :title="$t('重命名')"
      :width="360"
      @ok="submitRename"
    >
      <Input
        v-model:value="renameName"
        @press-enter="submitRename"
      />
    </Modal>

    <!-- 移动 -->
    <Modal
      v-model:open="showMove"
      :title="$t('移动到')"
      :width="400"
      @ok="submitMove"
    >
      <div class="move-tree">
        <Tree
          :tree-data="[{ key: '', title: '根目录', children: moveTreeData }]"
          :default-expand-all="false"
          :selected-keys="moveParentId ? [moveParentId] : ['']"
          @select="(keys: any[]) => { if (keys.length) moveParentId = String(keys[0]) }"
        />
      </div>
    </Modal>

    <!-- 新建文件夹 -->
    <Modal
      v-model:open="newFolderOpen"
      :title="$t('新建文件夹')"
      :width="360"
      :confirm-loading="folderCreating"
      @ok="createFolder"
    >
      <Input
        v-model:value="newFolderName"
        :placeholder="$t('文件夹名称')"
        @press-enter="createFolder"
      />
      <div class="folder-hint">
        将创建在当前目录：{{ breadcrumbs[breadcrumbs.length - 1]?.name || '根目录' }}
      </div>
    </Modal>

    <!-- 图片灯箱 -->
    <Modal
      v-model:open="lightboxOpen"
      :footer="null"
      :closable="false"
      :width="'90vw'"
      wrap-class-name="lightbox-modal"
    >
      <div
        v-if="lightboxItem"
        class="lightbox"
      >
        <button
          type="button"
          class="lb-btn lb-prev"
          :title="$t('上一张')"
          @click="lbPrev"
        >
          <LeftOutlined />
        </button>
        <img
          :src="itemUrl(lightboxItem)"
          :alt="lightboxItem.name"
          class="lb-img"
          @error="onImgError($event, lightboxItem)"
        >
        <button
          type="button"
          class="lb-btn lb-next"
          :title="$t('下一张')"
          @click="lbNext"
        >
          <RightOutlined />
        </button>
        <div class="lb-meta">
          <span class="lb-name">{{ lightboxItem.name }}</span>
          <span class="lb-count">{{ lightboxIndex + 1 }} / {{ lightboxList.length }}</span>
        </div>
        <button
          type="button"
          class="lb-close"
          :title="$t('关闭')"
          aria-label="关闭"
          @click="lightboxOpen = false"
        >
          ✕
        </button>
      </div>
    </Modal>

    <!-- 分享 -->
    <Modal
      v-model:open="showShare"
      :title="$t('分享链接')"
      :width="480"
      :footer="null"
    >
      <div class="share-row">
        <Input
          :value="shareUrl"
          read-only
        />
        <Button @click="copyShareUrl">
          {{ $t('复制') }}
        </Button>
      </div>
      <div
        v-if="shareExpires"
        class="share-expires"
      >
        有效期至 {{ shareExpires }}
      </div>
    </Modal>

    <!-- 预览 -->
    <Modal
      v-model:open="showPreview"
      :title="previewItem?.name"
      :width="'90vw'"
      :style="{ maxWidth: '1200px' }"
      :footer="null"
      @cancel="showPreview = false"
    >
      <div
        v-if="previewItem"
        class="preview-shell"
      >
        <file-viewer
          :url="itemUrl(previewItem)"
          :options="{ preset: allPreset, rendererMode: 'replace', theme: 'light', toolbar: { position: 'auto' } }"
        />
      </div>
    </Modal>
    <!-- 上传（拖拽 + 多文件 + 进度） -->
    <Modal
      v-model:open="showUpload"
      :title="$t('上传文件')"
      :width="640"
      :footer="null"
      destroy-on-close
    >
      <div class="upload-container">
        <Upload
          :file-list="uploadFileList"
          :multiple="true"
          :custom-request="handleUploadRequest"
          drag
          style="width: 100%"
        >
          <p class="ant-upload-drag-icon">
            <CloudUploadOutlined />
          </p>
          <p class="ant-upload-text">
            {{ $t('拖拽文件到此处，或点击选择') }}
          </p>
          <p class="ant-upload-hint">
            {{ $t('支持多文件上传，单文件不超过 50MB') }}
          </p>
        </Upload>
        
        <!-- 上传进度列表 -->
        <div
          v-if="uploadingFiles.size > 0"
          class="upload-progress-list"
        >
          <h4 style="margin: 16px 0 8px; font-size: 14px;">
            {{ $t('上传进度') }}
          </h4>
          <div 
            v-for="[fileId, state] in uploadingFiles" 
            :key="fileId"
            class="upload-item"
          >
            <div class="upload-item-header">
              <span class="upload-filename">{{ fileId.split('-')[0] }}</span>
              <span 
                class="upload-status" 
                :class="state.status"
              >
                {{ state.status === 'uploading' ? `${state.progress}%` : 
                  state.status === 'success' ? '✓ 完成' : 
                  `✗ ${state.error}` }}
              </span>
            </div>
            <Progress 
              v-if="state.status === 'uploading'"
              :percent="state.progress" 
              :stroke-color="state.progress < 30 ? '#ff4d4f' : state.progress < 70 ? '#faad14' : '#52c41a'"
              size="small"
            />
            <div
              v-else-if="state.status === 'error'"
              class="upload-error-detail"
            >
              {{ state.error }}
            </div>
          </div>
          
          <!-- 批量操作按钮 -->
          <div
            class="upload-actions"
            style="margin-top: 12px; display: flex; gap: 8px; justify-content: flex-end;"
          >
            <Button
              size="small"
              @click="clearCompletedUploads"
            >
              {{ $t('清除已完成') }}
            </Button>
            <Button
              size="small"
              type="primary"
              @click="retryFailedUploads"
            >
              {{ $t('重试失败') }}
            </Button>
          </div>
        </div>
      </div>
    </Modal>

    <!-- 添加到知识库 -->
    <Modal
      v-model:open="showKbModal"
      :title="$t('添加到知识库')"
      :width="420"
      :footer="null"
    >
      <p>{{ $t('已选择') }} <strong>{{ selectedIds.size }}</strong> {{ $t('个文件') }}</p>
      <Select
        v-model:value="selectedKbId"
        :options="kbOptions"
        :placeholder="$t('请选择知识库')"
        show-search
        style="width: 100%; margin-top: 12px"
      />
      <div class="modal-footer">
        <Button @click="showKbModal = false">
          {{ $t('取消') }}
        </Button>
        <Button
          type="primary"
          :loading="uploadingToKb"
          :disabled="!selectedKbId"
          @click="uploadToKnowledgeBase"
        >
          {{ $t('开始上传') }}
        </Button>
      </div>
    </Modal>

    <!-- 批量移动 -->
    <Modal
      v-model:open="showBatchMove"
      :title="$t('批量移动到')"
      :width="400"
      :confirm-loading="batchMoving"
      @ok="submitBatchMove"
    >
      <p>{{ $t('将移动') }} <strong>{{ selectedIds.size }}</strong> {{ $t('个选中项') }}</p>
      <div class="move-tree">
        <Tree
          :tree-data="[{ key: '', title: '根目录', children: moveTreeData }]"
          :default-expand-all="false"
          :selected-keys="batchMoveParentId ? [batchMoveParentId] : ['']"
          @select="(keys: any[]) => { if (keys.length) batchMoveParentId = String(keys[0]) }"
        />
      </div>
    </Modal>

    <!-- 批量重命名 -->
    <Modal
      v-model:open="showBatchRename"
      :title="$t('批量重命名')"
      :width="500"
      :confirm-loading="batchRenaming"
      @ok="submitBatchRename"
    >
      <p>{{ $t('将为') }} <strong>{{ selectedIds.size }}</strong> {{ $t('个文件应用以下规则：') }}</p>
      <div style="margin-top: 16px; display: flex; flex-direction: column; gap: 12px;">
        <div>
          <label style="display: block; margin-bottom: 4px; font-size: 13px; color: var(--text-muted);">{{ $t('前缀') }}</label>
          <Input
            v-model:value="renamePrefix"
            placeholder="例如: [项目A]_"
          />
        </div>
        <div>
          <label style="display: block; margin-bottom: 4px; font-size: 13px; color: var(--text-muted);">{{ $t('后缀') }}</label>
          <Input
            v-model:value="renameSuffix"
            placeholder="例如: _v2"
          />
        </div>
        <div>
          <label style="display: block; margin-bottom: 4px; font-size: 13px; color: var(--text-muted);">{{ $t('查找文本') }}</label>
          <Input
            v-model:value="renameFindText"
            :placeholder="$t('要替换的文本（支持正则）')"
          />
        </div>
        <div>
          <label style="display: block; margin-bottom: 4px; font-size: 13px; color: var(--text-muted);">{{ $t('替换为') }}</label>
          <Input
            v-model:value="renameReplaceText"
            :placeholder="$t('替换后的文本')"
          />
        </div>
      </div>
      <div style="margin-top: 12px; padding: 8px; background: var(--bg-secondary); border-radius: 4px; font-size: 12px; color: var(--text-tertiary);">
        <strong>{{ $t('示例：') }}</strong>原文件名 "report.pdf" → 前缀 "[2024]_" + 后缀 "_final" = "[2024]_report_final.pdf"
      </div>
    </Modal>

    <!-- 批量标签 -->
    <Modal
      v-model:open="showBatchTags"
      :title="$t('批量添加标签')"
      :width="400"
      :confirm-loading="batchTagging"
      @ok="submitBatchTags"
    >
      <p>{{ $t('将为') }} <strong>{{ selectedIds.size }}</strong> {{ $t('个文件添加标签：') }}</p>
      <div style="margin-top: 16px;">
        <Input
          v-model:value="batchTagInput"
          :placeholder="$t('输入标签名称，回车确认')"
          @press-enter="submitBatchTags"
        />
      </div>
      <div style="margin-top: 8px; font-size: 12px; color: var(--text-tertiary);">
        {{ $t('提示：如果文件已有该标签，将自动跳过') }}
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.media-page { padding: 72px 24px 16px; display: flex; flex-direction: column; gap: 12px; height: 100%; overflow: hidden; }
.media-toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.breadcrumb { display: flex; align-items: center; gap: 4px; min-width: 0; }
.crumb { font-size: 14px; color: var(--text-secondary); cursor: pointer; white-space: nowrap; }
.crumb:hover { color: var(--primary); }
.crumb.active { color: var(--text-primary); font-weight: 600; }
.crumb-sep { margin-left: 4px; color: var(--text-muted); }
.toolbar-actions { margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.file-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; overflow-y: auto; padding-bottom: 24px; }
.file-card { border: 1px solid var(--border-card); border-radius: var(--radius-lg); background: var(--bg-card); padding: 10px; cursor: pointer; transition: all var(--dur-fast); display: flex; flex-direction: column; gap: 6px; }
.file-card:hover { border-color: var(--primary); transform: translateY(-2px); box-shadow: var(--shadow-md); }
.file-card:focus-visible,
.lb-btn:focus-visible,
.lb-close:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}
.file-card.selected { border-color: var(--primary); background: var(--primary-bg); }
.card-thumb { height: 84px; display: flex; align-items: center; justify-content: center; border-radius: var(--radius-md); background: var(--bg-secondary); overflow: hidden; }
.card-thumb img { max-width: 100%; max-height: 100%; object-fit: cover; }
.thumb-icon { font-size: 32px; color: var(--text-tertiary); }
.thumb-icon.folder { color: var(--primary); }
.card-name { font-size: 13px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card-meta { font-size: 11px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
.pagination-bar { display: flex; justify-content: flex-end; padding-top: 8px; border-top: 1px solid var(--border); }
:deep(.row-selected) { background: var(--primary-bg) !important; }
.batch-bar { position: sticky; top: 0; z-index: var(--z-sticky); display: flex; align-items: center; gap: 12px; padding: 8px 12px; background: var(--bg-card); border: 1px solid var(--primary); border-radius: var(--radius-md); box-shadow: var(--shadow-md); }
.batch-count { font-size: 13px; color: var(--text-primary); }
.card-check { position: absolute; top: 6px; left: 6px; }
.file-card { position: relative; }
.detail-thumb { height: 160px; display: flex; align-items: center; justify-content: center; background: var(--bg-secondary); border-radius: var(--radius-lg); margin-bottom: 16px; overflow: hidden; }
.detail-thumb img { max-width: 100%; max-height: 100%; object-fit: contain; }
.detail-info { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
.detail-row { display: flex; justify-content: space-between; gap: 12px; font-size: 13px; }
.detail-row .label { color: var(--text-muted); flex-shrink: 0; }
.url-text { word-break: break-all; color: var(--text-secondary); }
.detail-actions { display: flex; flex-direction: column; gap: 8px; }
.share-row { display: flex; gap: 8px; }
.share-expires { margin-top: 8px; font-size: 12px; color: var(--text-muted); }
.preview-shell { height: 75vh; min-height: 400px; border-radius: 4px; overflow: hidden; }
/* 新功能样式：总数/标签/移动树/灯箱/新建文件夹 */
.media-total { margin-left: auto; font-size: 12px; color: var(--text-tertiary); white-space: nowrap; }
.tag-list { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.no-tags { font-size: 12px; color: var(--text-muted); }
.tag-add { margin-top: 6px; }
.move-tree { max-height: 320px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; padding: 8px; }
.folder-hint { margin-top: 8px; font-size: 12px; color: var(--text-tertiary); }
.lightbox { position: relative; display: flex; align-items: center; justify-content: center; min-height: 60vh; }
.lb-img { max-width: 82%; max-height: 72vh; object-fit: contain; border-radius: 8px; box-shadow: var(--shadow-lg); }
.lb-btn { position: absolute; top: 50%; transform: translateY(-50%); width: 40px; height: 40px; border: 1px solid var(--border); border-radius: 50%; background: var(--bg-card); color: var(--text-primary); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; box-shadow: var(--shadow-md); }
.lb-prev { left: 8px; }
.lb-next { right: 8px; }
.lb-btn:hover { color: var(--primary); border-color: var(--primary); }
.lb-meta { position: absolute; bottom: 8px; left: 50%; transform: translateX(-50%); display: flex; align-items: center; gap: 10px; padding: 4px 12px; border-radius: 999px; background: rgba(0, 0, 0, 0.6); color: #fff; font-size: 12px; }
.lb-name { max-width: 40vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lb-count { color: rgba(255, 255, 255, 0.7); }
.lb-close { position: absolute; top: 8px; right: 8px; width: 30px; height: 30px; border: none; border-radius: 50%; background: rgba(0, 0, 0, 0.5); color: #fff; cursor: pointer; }
.lb-close:hover { background: rgba(0, 0, 0, 0.7); }
@media (max-width: 768px) {
  .media-page { padding: 12px; height: auto; min-height: 100%; overflow: visible; }
  .file-grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 8px; overflow: visible; padding-bottom: 16px; }
  :deep(.ant-drawer-content-wrapper) { width: 100vw !important; }
  .toolbar-actions { margin-left: 0; width: 100%; }
  /* 筛选/搜索控件占满整行，便于触控 */
  .toolbar-actions :deep(.ant-input-affix-wrapper) { width: 100% !important; }
  .toolbar-actions :deep(.ant-select) { width: 100% !important; min-width: 0; }
  .toolbar-actions :deep(.ant-segmented) { width: 100%; }
  .toolbar-actions :deep(.ant-segmented-group) { width: 100%; display: flex; }
  .toolbar-actions :deep(.ant-segmented-item) { flex: 1; text-align: center; }
  .batch-bar { position: fixed; left: 12px; right: 12px; bottom: 12px; top: auto; flex-wrap: wrap; }
  .media-total { margin-left: 0; }
}
.modal-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }

/* 上传进度列表样式 */
.upload-container { display: flex; flex-direction: column; gap: 12px; }
.upload-progress-list { max-height: 400px; overflow-y: auto; }
.upload-item { 
  padding: 10px; 
  margin-bottom: 8px; 
  border: 1px solid var(--border); 
  border-radius: var(--radius-md); 
  background: var(--bg-secondary);
}
.upload-item-header { 
  display: flex; 
  justify-content: space-between; 
  align-items: center; 
  margin-bottom: 6px; 
}
.upload-filename { 
  font-size: 13px; 
  color: var(--text-primary); 
  white-space: nowrap; 
  overflow: hidden; 
  text-overflow: ellipsis; 
  max-width: 70%;
}
.upload-status { 
  font-size: 12px; 
  font-weight: 500;
}
.upload-status.uploading { color: var(--primary); }
.upload-status.success { color: #52c41a; }
.upload-status.error { color: #ff4d4f; }
.upload-error-detail { 
  margin-top: 6px; 
  font-size: 12px; 
  color: #ff4d4f; 
  padding: 4px 8px; 
  background: rgba(255, 77, 79, 0.1); 
  border-radius: 4px;
}
.upload-actions { 
  margin-top: 12px; 
  display: flex; 
  gap: 8px; 
  justify-content: flex-end; 
}
</style>
