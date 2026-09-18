<script setup lang="ts">
import { ref, onMounted, watch, computed, nextTick } from 'vue'
import { Input, Button, Select, message } from 'ant-design-vue'
import { SendOutlined, StopOutlined, PaperClipOutlined, CloseOutlined, FileOutlined, BranchesOutlined, AudioOutlined, FolderOpenOutlined } from '@ant-design/icons-vue'
import { uploadFile, listModels } from '../../api'
import type { LlmModel } from '../../api'
import type { ChatAttachment } from './chat-types'
import MediaPickerDialog from './MediaPickerDialog.vue'

import { useI18n } from 'vue-i18n'
const { t: tr } = useI18n()
const props = defineProps<{
  loading: boolean
  mode: string
  modeOptions: { label: string; value: string }[]
  disabled?: boolean
  /** 当前会话 ID，用于按会话持久化草稿 */
  sessionId?: string
  /** 模型路由：当前会话 llm_config.model（空 = 后端默认路由） */
  model?: string
}>()

const emit = defineEmits<{
  (e: 'send', text: string, attachments?: ChatAttachment[]): void
  (e: 'stop'): void
  (e: 'update:mode', mode: string): void
  /** 模型路由：用户选择了模型（空字符串 = 恢复后端默认） */
  (e: 'model-change', model: string): void
  /** 斜杠命令 */
  (e: 'command', cmd: string): void
  /** 上下文快捷按钮：展开侧栏（若为抽屉模式） */
  (e: 'open-panel'): void
}>()

const input = ref('')
const textareaRef = ref()
const fileInputRef = ref<HTMLInputElement | null>(null)
const dragOver = ref(false)

// 附件管理
const pendingAttachments = ref<ChatAttachment[]>([])
const uploading = ref(false)

// 从媒体库选择：复用「媒体库」页面已有的 media_assets 资产作为附件
const showMediaPicker = ref(false)

function onMediaPicked(atts: ChatAttachment[]) {
  const existing = new Set(pendingAttachments.value.map(a => a.id))
  for (const a of atts) {
    if (!existing.has(a.id)) pendingAttachments.value.push(a)
  }
}

// 允许的文件类型（图片 + 常见文档）
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml']
const AUDIO_TYPES = ['audio/mpeg', 'audio/wav', 'audio/ogg', 'audio/flac', 'audio/webm', 'audio/mp4', 'audio/x-m4a', 'audio/aac']
const MAX_FILE_SIZE = 50 * 1024 * 1024 // 50MB

// ── 模型路由选择器（GET /v1/models，仅 enabled） ──
const models = ref<LlmModel[]>([])
const modelsLoading = ref(false)
const modelValue = ref('')
const modelOptions = computed(() =>
  models.value.map(m => ({
    label: m.display_name ? `${m.provider} · ${m.display_name}` : `${m.provider} · ${m.name}`,
    value: m.name,
  })),
)

function onModelChange(v: any) {
  modelValue.value = String(v || '')
  emit('model-change', modelValue.value)
}

// 会话切换/恢复时同步（父组件回传 llm_config.model；空 = 后端默认）
watch(() => props.model, (v) => { modelValue.value = v || '' }, { immediate: true })

// 挂载自动聚焦 + 加载可用模型
onMounted(async () => {
  textareaRef.value?.focus?.()
  try {
    modelsLoading.value = true
    models.value = await listModels()
  } catch {
    models.value = []
  } finally {
    modelsLoading.value = false
  }
})

// ── 本地存储（草稿 / 历史共用）：隐私模式下访问 localStorage 会抛异常，静默降级 ──
function readLocal(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}
function writeLocal(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch { /* 配额满 / 被禁用：放弃持久化，本次会话内的输入不受影响 */ }
}

// ── 输入历史召回（↑/↓，shell 语义；Esc 放弃召回并恢复草稿） ──
// 与会话无关：它记的是「我刚问过什么」，跨会话沿用才是符合直觉的行为。
const HISTORY_KEY = 'chiron:composer-history:v1'
const HISTORY_LIMIT = 50
const history = ref<string[]>(loadHistory())
/** 召回游标：null = 未在召回（显示自己写的草稿） */
const historyCursor = ref<number | null>(null)
/** 首次召回前的草稿，用于「越过最近一条」时回到自己写的内容 */
let draftBeforeRecall = ''

function loadHistory(): string[] {
  const raw = readLocal(HISTORY_KEY)
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : []
  } catch {
    return []
  }
}

function pushHistory(text: string) {
  // 重复提问移到最近，而不是堆第二份
  const next = history.value.filter(v => v !== text)
  next.push(text)
  history.value = next.slice(-HISTORY_LIMIT)
  writeLocal(HISTORY_KEY, JSON.stringify(history.value))
}

function recalledText(): string | null {
  const cursor = historyCursor.value
  return cursor === null ? null : history.value[cursor] ?? null
}

/** 返回是否消费了这次按键 */
function recallHistory(delta: 1 | -1): boolean {
  if (!history.value.length) return false
  const cursor = historyCursor.value
  if (cursor === null) {
    if (delta === 1) return false            // 还没进入召回，没有「下一条」可去
    draftBeforeRecall = input.value
    historyCursor.value = history.value.length - 1
  } else {
    const next = cursor + delta
    if (next >= history.value.length) {      // 越过最近一条 → 回到自己写的草稿
      historyCursor.value = null
      input.value = draftBeforeRecall
      return true
    }
    if (next < 0) return true                // 已到最早一条：停住，不绕回
    historyCursor.value = next
  }
  input.value = history.value[historyCursor.value!] ?? ''
  return true
}

function cancelRecall(): boolean {
  if (historyCursor.value === null) return false
  historyCursor.value = null
  input.value = draftBeforeRecall
  return true
}

/** 不在首行时 ↑/↓ 属于光标移动，不召回 */
function caretOnFirstLine(el: HTMLTextAreaElement): boolean {
  const caret = el.selectionStart ?? 0
  return !el.value.slice(0, caret).includes('\n')
}

// 草稿持久化（按会话 ID 存 localStorage）
const DRAFT_PREFIX = 'chiron:draft:'
function draftKey(sid?: string) { return sid ? DRAFT_PREFIX + sid : '' }
function loadDraft() {
  const key = draftKey(props.sessionId)
  input.value = key ? (readLocal(key) || '') : ''
  historyCursor.value = null
}
function saveDraft() {
  const key = draftKey(props.sessionId)
  if (!key) return
  writeLocal(key, input.value || null)
}
// 会话切换时加载草稿
watch(() => props.sessionId, () => loadDraft(), { immediate: true })
// 输入变化时保存草稿（防抖避免频繁写入）；手动编辑即退出历史召回
let saveTimer: ReturnType<typeof setTimeout> | null = null
watch(input, value => {
  if (value !== recalledText()) historyCursor.value = null
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = setTimeout(saveDraft, 300)
})

function onKeydown(e: KeyboardEvent) {
  // 斜杠命令面板导航
  if (showSlashMenu.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      slashIndex.value = Math.min(slashIndex.value + 1, filteredCommands.value.length - 1)
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      slashIndex.value = Math.max(slashIndex.value - 1, 0)
      return
    }
    if (e.key === 'Escape') {
      showSlashMenu.value = false
      return
    }
  }
  // ↑/↓ 召回历史（shell 语义）：斜杠菜单打开时不抢键；不在首行时留给光标移动
  if (!showSlashMenu.value && !e.altKey && !e.ctrlKey && !e.metaKey
    && (e.key === 'ArrowUp' || e.key === 'ArrowDown')
    && caretOnFirstLine(e.target as HTMLTextAreaElement)) {
    if (recallHistory(e.key === 'ArrowUp' ? -1 : 1)) {
      e.preventDefault()
      return
    }
  }
  if (e.key === 'Escape' && cancelRecall()) return
  // Enter 发送（无修饰键）；Shift+Enter 换行；Cmd/Ctrl+Enter 也发送（兼容）
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    // 如果在斜杠菜单上且选中了命令，执行命令而非发送
    if (showSlashMenu.value && filteredCommands.value[slashIndex.value]) {
      const cmd = filteredCommands.value[slashIndex.value].cmd
      input.value = ''
      showSlashMenu.value = false
      emit('command', cmd)
      return
    }
    submit()
  }
}

function submit() {
  const text = input.value.trim()
  if ((!text && !pendingAttachments.value.length) || props.loading) return
  const atts = pendingAttachments.value.length ? [...pendingAttachments.value] : undefined
  if (text) pushHistory(text)
  historyCursor.value = null
  input.value = ''
  pendingAttachments.value = []
  // 发送后清空草稿
  saveDraft()
  emit('send', text, atts)
}

// 文件选择
function triggerFilePick() {
  fileInputRef.value?.click()
}

async function handleFiles(files: FileList | File[]) {
  const arr = Array.from(files)
  if (!arr.length) return
  uploading.value = true
  try {
    for (const file of arr) {
      if (file.size > MAX_FILE_SIZE) {
        message.error(`${file.name} 超过 50MB 限制`)
        continue
      }
      const result = await uploadFile(file)
      pendingAttachments.value.push({
        id: result.id,
        name: result.name,
        size: result.size,
        mimeType: result.mimeType,
        url: result.url,
        isImage: IMAGE_TYPES.includes(result.mimeType),
        isAudio: AUDIO_TYPES.includes(result.mimeType),
      })
    }
  } catch (e: any) {
    message.error('文件上传失败: ' + (e.message || tr('网络错误')))
  } finally {
    uploading.value = false
    if (fileInputRef.value) fileInputRef.value.value = ''
  }
}

function onFileChange(e: Event) {
  const target = e.target as HTMLInputElement
  if (target.files?.length) handleFiles(target.files)
}

// 拖拽上传
function onDragOver(e: DragEvent) {
  e.preventDefault()
  dragOver.value = true
}
function onDragLeave(e: DragEvent) {
  e.preventDefault()
  dragOver.value = false
}
function onDrop(e: DragEvent) {
  e.preventDefault()
  dragOver.value = false
  if (e.dataTransfer?.files?.length) handleFiles(e.dataTransfer.files)
}

// 粘贴图片
function onPaste(e: ClipboardEvent) {
  const items = e.clipboardData?.items
  if (!items) return
  const files: File[] = []
  for (const it of items) {
    if (it.kind === 'file') {
      const f = it.getAsFile()
      if (f) files.push(f)
    }
  }
  if (files.length) handleFiles(files)
  // 大文本粘贴折叠预览
  const text = e.clipboardData?.getData('text')
  if (text && text.length > 1000) {
    e.preventDefault()
    pastedLargeText.value = text
  }
}

// 大文本粘贴折叠预览
const pastedLargeText = ref('')
const PASTE_PREVIEW = 200
function acceptPastedText() {
  input.value += pastedLargeText.value
  pastedLargeText.value = ''
  nextTick(() => textareaRef.value?.focus?.())
}
function discardPastedText() {
  pastedLargeText.value = ''
}

function removeAttachment(id: string) {
  const idx = pendingAttachments.value.findIndex(a => a.id === id)
  if (idx >= 0) pendingAttachments.value.splice(idx, 1)
}

// ── 录音 ──
const recording = ref(false)
const recordingTimer = ref(0)
let recordingInterval: ReturnType<typeof setInterval> | null = null
let mediaRecorder: MediaRecorder | null = null
let audioChunks: Blob[] = []

async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia) {
    message.error(tr('浏览器不支持录音'))
    return
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    audioChunks = []
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/webm'
    mediaRecorder = new MediaRecorder(stream, { mimeType })
    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data)
    }
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop())
      const blob = new Blob(audioChunks, { type: mimeType })
      if (blob.size < 100) return // 太短不处理
      const file = new File([blob], `recording_${Date.now()}.webm`, { type: mimeType })
      uploading.value = true
      try {
        const result = await uploadFile(file)
        pendingAttachments.value.push({
          id: result.id,
          name: '🎤 ' + result.name,
          size: result.size,
          mimeType: result.mimeType,
          url: result.url,
          isImage: false,
        })
      } catch (e: any) {
        message.error('录音上传失败: ' + (e.message || tr('网络错误')))
      } finally {
        uploading.value = false
      }
    }
    mediaRecorder.start()
    recording.value = true
    recordingTimer.value = 0
    recordingInterval = setInterval(() => { recordingTimer.value++ }, 1000)
  } catch {
    message.error(tr('无法访问麦克风，请检查权限设置'))
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop()
  }
  recording.value = false
  if (recordingInterval) {
    clearInterval(recordingInterval)
    recordingInterval = null
  }
}

function formatRecordingTime(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}

// ── 斜杠命令 ──
const SLASH_COMMANDS = [
  { cmd: '/clear', desc: '清空当前对话' },
  { cmd: '/export', desc: '导出当前会话为 Markdown' },
  { cmd: '/new', desc: '新建会话' },
  { cmd: '/theme', desc: '切换暗色/亮色模式' },
  { cmd: '/stop', desc: '停止生成' },
]
const showSlashMenu = ref(false)
const slashIndex = ref(0)
const filteredCommands = computed(() => {
  const q = input.value.trim().toLowerCase()
  if (!q.startsWith('/')) return []
  return SLASH_COMMANDS.filter(c => c.cmd.startsWith(q))
})
function onSlashInput() {
  showSlashMenu.value = filteredCommands.value.length > 0
  slashIndex.value = 0
}

/**
 * 供父组件引用选中内容（消息区「引用到输入框」）：
 * 用 `>` 引用块保留原文换行，插入后把光标交回输入框。
 */
function insertText(text: string) {
  const quoted = text.split('\n').map(line => `> ${line}`).join('\n')
  input.value = input.value ? `${input.value}\n\n${quoted}\n\n` : `${quoted}\n\n`
  historyCursor.value = null
  nextTick(() => textareaRef.value?.focus?.())
}

defineExpose({ insertText })
</script>

<template>
  <div
    class="input-area"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <div
      class="input-card"
      :class="{ 'drag-active': dragOver }"
    >
      <!-- 大文本粘贴折叠预览 -->
      <div
        v-if="pastedLargeText"
        class="paste-preview"
      >
        <div class="paste-preview-text">
          {{ pastedLargeText.slice(0, PASTE_PREVIEW) }}<span v-if="pastedLargeText.length > PASTE_PREVIEW">…</span>
        </div>
        <div class="paste-preview-meta">
          已粘贴 {{ pastedLargeText.length }} 字符
        </div>
        <div class="paste-preview-actions">
          <button
            class="paste-btn discard"
            type="button"
            @click="discardPastedText"
          >
            {{ $t('丢弃') }}
          </button>
          <button
            class="paste-btn accept"
            type="button"
            @click="acceptPastedText"
          >
            {{ $t('插入') }}
          </button>
        </div>
      </div>
      <!-- 斜杠命令面板 -->
      <div
        v-if="showSlashMenu"
        class="slash-menu"
        role="listbox"
      >
        <div
          v-for="(c, i) in filteredCommands"
          :key="c.cmd"
          class="slash-item"
          :class="{ active: i === slashIndex }"
          role="option"
          :aria-selected="i === slashIndex"
          @mouseenter="slashIndex = i"
          @click="emit('command', c.cmd); input = ''; showSlashMenu = false"
        >
          <span class="slash-cmd">{{ c.cmd }}</span>
          <span class="slash-desc">{{ c.desc }}</span>
        </div>
      </div>
      <!-- 附件预览区 -->
      <div
        v-if="pendingAttachments.length"
        class="attachment-preview"
      >
        <div
          v-for="att in pendingAttachments"
          :key="att.id"
          class="att-thumb"
        >
          <img
            v-if="att.isImage"
            :src="att.url"
            :alt="att.name"
            class="att-thumb-img"
          >
          <div
            v-else
            class="att-thumb-file"
          >
            <FileOutlined />
            <span class="att-thumb-name">{{ att.name }}</span>
          </div>
          <button
            class="att-remove"
            type="button"
            :title="$t('移除')"
            @click="removeAttachment(att.id)"
          >
            <CloseOutlined />
          </button>
        </div>
      </div>
      <Input.TextArea
        ref="textareaRef"
        v-model:value="input"
        :rows="1"
        :auto-size="{ minRows: 1, maxRows: 5 }"
        :placeholder="dragOver ? '松开以上传文件' : '发送消息...（/ 查看命令 · ↑ 召回历史）'"
        class="input-field"
        :disabled="disabled"
        aria-label="消息输入框"
        @keydown="onKeydown"
        @input="onSlashInput"
        @paste="onPaste"
      />
      <input
        ref="fileInputRef"
        type="file"
        multiple
        style="display: none"
        @change="onFileChange"
      >
      <div class="input-actions">
        <div class="input-left">
          <Button
            type="text"
            size="small"
            class="attach-btn"
            :loading="uploading"
            :title="$t('上传文件')"
            @click="triggerFilePick"
          >
            <template #icon>
              <PaperClipOutlined />
            </template>
          </Button>
          <Button
            type="text"
            size="small"
            class="attach-btn"
            :title="$t('从媒体库选取')"
            @click="showMediaPicker = true"
          >
            <template #icon>
              <FolderOpenOutlined />
            </template>
          </Button>
          <Button
            type="text"
            size="small"
            class="record-btn"
            :class="{ recording: recording }"
            :title="recording ? '点击停止录音' : '语音输入'"
            @click="recording ? stopRecording() : startRecording()"
          >
            <template #icon>
              <AudioOutlined />
            </template>
            <span v-if="recording" class="recording-timer">{{ formatRecordingTime(recordingTimer) }}</span>
          </Button>
          <span class="mode-label">{{ $t('模式') }}</span>
          <Select
            :model-value="mode"
            :options="modeOptions"
            size="small"
            style="width: 110px"
            :title="`当前模式：${modeOptions.find(o => o.value === mode)?.label || mode}（仅影响后续消息）`"
            @update:value="(v: any) => emit('update:mode', String(v))"
          />
          <span class="mode-label">{{ $t('模型') }}</span>
          <Select
            class="model-select"
            :model-value="modelValue"
            :options="modelOptions"
            :loading="modelsLoading"
            size="small"
            allow-clear
            :placeholder="$t('默认模型')"
            :title="`当前模型：${modelValue || '默认（后端路由）'}（仅影响后续消息）`"
            @update:value="onModelChange"
          />
          <Button
            type="text"
            size="small"
            class="context-btn"
            :title="$t('打开上下文面板（会话/轨迹/上下文）')"
            @click="emit('open-panel')"
          >
            <template #icon>
              <BranchesOutlined />
            </template>
            <span class="context-label">{{ $t('上下文') }}</span>
          </Button>
        </div>
        <div class="input-left">
          <span class="input-hint">{{ $t('Enter 发送 · Shift+Enter 换行') }}</span>
          <Button
            class="send-btn"
            :type="loading ? 'default' : 'primary'"
            shape="circle"
            :disabled="(!input.trim() && !pendingAttachments.length && !loading) || disabled"
            :title="loading ? '停止' : '发送'"
            @click="loading ? emit('stop') : submit()"
          >
            <template #icon>
              <StopOutlined v-if="loading" />
              <SendOutlined v-else />
            </template>
          </Button>
        </div>
      </div>
    </div>
  </div>
  <MediaPickerDialog
    v-model:open="showMediaPicker"
    @select="onMediaPicked"
  />
</template>

<style scoped>
/* 浮动胶囊输入卡（deepseek InputBar floating capsule）12px 圆角 + 阴影 + 16/24 字号 */
.input-area { padding: 0 16px 8px; }
.input-card {
  position: relative;
  display: flex; flex-direction: column; gap: 12px;
  width: 100%; max-width: var(--chat-content-width); margin: 0 auto;
  padding: 10px 12px 12px;
  border: var(--sig-border-width) solid var(--border); border-radius: var(--sig-radius-input);
  background: var(--bg-input); box-shadow: var(--sig-input-shadow);
  font-size: 16px; line-height: 24px;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
/* 聚焦态：主色描边 + 主色光晕 */
.input-card:focus-within {
  border-color: var(--primary);
  box-shadow: var(--sig-input-shadow), 0 0 0 var(--sig-input-ring) var(--primary-bg);
}
.input-field { background: transparent !important; }
.input-field :deep(textarea) { color: var(--text-primary) !important; font-size: 16px !important; line-height: 24px !important; }
.input-actions { display: flex; align-items: center; justify-content: space-between; }
.input-left { display: flex; align-items: center; gap: 8px; }
.input-hint { font-size: 12px; color: var(--text-tertiary); }
/* 模式选择器标签 */
.mode-label { flex: none; font-size: 12px; color: var(--text-tertiary); }
/* 模型路由下拉（与模式切换器同排；窄屏收缩防溢出） */
.model-select { width: 170px; }
.model-select :deep(.ant-select-selector) { font-size: 12px; }
@media (max-width: 768px) { .model-select { width: 150px; } }
@media (max-width: 576px) { .model-select { width: 126px; } }
/* 上下文快捷按钮：展开侧栏（抽屉模式） */
.context-btn { color: var(--text-tertiary); display: inline-flex; align-items: center; gap: 4px; }
.context-btn:hover { color: var(--primary) !important; }
.context-label { font-size: 12px; }
@media (max-width: 576px) {
  .context-label { display: none; }
  .context-btn.ant-btn { min-width: 40px; height: 40px; }
}
/* 发送按钮：可发送时主色、hover 微放大 + 加深 */
.send-btn { transition: transform 0.15s ease, box-shadow 0.15s ease, opacity 0.15s ease; }
.send-btn:not(:disabled):hover { transform: scale(1.06); box-shadow: 0 4px 12px var(--primary-bg); filter: brightness(1.05); }
.send-btn:disabled { opacity: 0.45; }
@media (max-width: 768px) { .input-area { padding: 0 12px 8px; } }
/* ── 移动端：输入区贴底 + 安全区 + 触控目标放大 + 工具栏换行 ── */
@media (max-width: 768px) {
  .input-area { padding: 0 12px calc(8px + env(safe-area-inset-bottom)); }
  .input-card { border-radius: var(--sig-radius-input); }
  .input-actions { gap: 8px; flex-wrap: wrap; row-gap: 6px; }
  .input-hint { display: none; } /* 窄屏隐藏提示文字，占位符承担语义 */
}
@media (max-width: 576px) {
  .input-area { padding: 0 8px calc(8px + env(safe-area-inset-bottom)); }
  .input-card { padding: 8px 10px 10px; gap: 10px; }
  .input-actions { flex-wrap: wrap; row-gap: 6px; }
  .input-left { gap: 4px; }
  .input-left:last-child { margin-left: auto; } /* 发送组靠右，避免与左侧工具组抢行 */
  .attach-btn.ant-btn { min-width: 40px; height: 40px; }
  .send-btn.ant-btn { width: 40px; height: 40px; }
  .paste-btn { min-height: 36px; }
}

/* 附件预览区 */
.attachment-preview { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 0 8px; }
.att-thumb { position: relative; width: 64px; height: 64px; border-radius: var(--sig-radius-card); border: 1px solid var(--border); background: var(--bg-card); overflow: hidden; }
.att-thumb-img { width: 100%; height: 100%; object-fit: cover; }
.att-thumb-file { width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; padding: 4px; color: var(--text-tertiary); font-size: 10px; }
.att-thumb-name { max-width: 56px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.att-remove { position: absolute; top: 2px; right: 2px; width: 18px; height: 18px; border-radius: 50%; border: none; background: var(--bg-overlay); color: #fff; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 10px; }
.att-remove:hover { background: var(--error); }
.attach-btn { color: var(--text-tertiary); display: inline-flex; align-items: center; justify-content: center; }
.attach-btn:hover { color: var(--primary); }
/* 录音按钮 */
.record-btn { color: var(--text-tertiary); display: inline-flex; align-items: center; justify-content: center; gap: 4px; }
.record-btn:hover { color: var(--primary); }
.record-btn.recording { color: var(--error); animation: pulse-rec 1.2s ease-in-out infinite; }
@keyframes pulse-rec {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.recording-timer { font-size: 12px; font-variant-numeric: tabular-nums; min-width: 32px; }
/* 拖拽态：边框主色 + 背景淡色 */
.input-card.drag-active { border-color: var(--primary); background: var(--primary-bg); box-shadow: var(--shadow-md), 0 0 0 3px var(--primary-bg); }

/* 斜杠命令面板 */
.slash-menu { position: absolute; bottom: 100%; left: 0; right: 0; margin-bottom: 4px; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--sig-radius-card); box-shadow: var(--shadow-lg); overflow: hidden; z-index: 10; }
.slash-item { display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; cursor: pointer; transition: background 0.1s ease; }
.slash-item.active { background: var(--bg-hover); }
.slash-cmd { font-weight: 600; color: var(--primary); font-size: 13px; }
.slash-desc { color: var(--text-tertiary); font-size: 12px; }

/* 大文本粘贴折叠预览 */
.paste-preview { border: 1px solid var(--border); border-radius: var(--sig-radius-card); padding: 8px 12px; background: var(--bg-secondary); margin-bottom: 4px; }
.paste-preview-text { font-size: 12px; color: var(--text-secondary); line-height: 1.5; max-height: 80px; overflow: hidden; white-space: pre-wrap; word-break: break-all; }
.paste-preview-meta { font-size: 11px; color: var(--text-tertiary); margin: 4px 0; }
.paste-preview-actions { display: flex; justify-content: flex-end; gap: 8px; }
.paste-btn { padding: 2px 10px; border-radius: var(--sig-radius-button); border: 1px solid var(--border); background: var(--bg-card); color: var(--text-secondary); font-size: 12px; cursor: pointer; }
.paste-btn.accept { background: var(--primary); color: #fff; border-color: var(--primary); }
.paste-btn.accept:hover { opacity: 0.9; }
.paste-btn.discard:hover { color: var(--error); border-color: var(--error); }
</style>