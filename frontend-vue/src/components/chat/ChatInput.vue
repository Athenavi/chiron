<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, computed, nextTick, h } from 'vue'
import { Input, Button, Select, Tooltip, Popover, message } from 'ant-design-vue'
import { SendOutlined, StopOutlined, PaperClipOutlined, CloseOutlined, FileOutlined, BranchesOutlined, AudioOutlined, FolderOpenOutlined, SettingOutlined } from '@ant-design/icons-vue'
import { uploadFile, listModels } from '../../api'
import type { LlmModel } from '../../api'
import type { ChatAttachment } from './chat-types'
import type { ContextChip } from './contextChips'
import MediaPickerDialog from './MediaPickerDialog.vue'
import { useAuthStore } from '../../stores/auth'
import { privateKey } from '../../utils/privateStorage'

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
  /** 工具授权模式（ask/auto/yolo）：控制工具执行是否需要用户确认 */
  toolsMode?: string
  /** 已带入本次对话的上下文（知识库 / Agent / 技能 / 工作流 / 插件 / 记忆分类）。
      对齐 reasonix 的 `composer-context`：绑定是"沉默生效"的，不显式呈现就是幽灵行为。 */
  contextChips?: ContextChip[]
}>()

const emit = defineEmits<{
  (e: 'send', text: string, attachments?: ChatAttachment[]): void
  (e: 'stop'): void
  /** `@` 提及选中的资源：以「引用 chip」形式交给父组件（与 URL chips 同一条链路） */
  (e: 'mention-add', payload: { type: string; id: string; name: string }): void
  (e: 'update:mode', mode: string): void
  /** 模型路由：用户选择了模型（空字符串 = 恢复后端默认） */
  (e: 'model-change', model: string): void
  /** 工具授权模式：ask=写类需确认 / auto=仅危险 / yolo=全跳过 */
  (e: 'tools-mode-change', mode: string): void
  /** 移除一个上下文 chip（点输入区上方 chip 行的 × ） */
  (e: 'remove-context-chip', chip: ContextChip): void
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

// ── 工具授权（ask/auto/yolo） ──
//
// 与「对话模式」(normal/minimal/ptc/creative) 是两个维度：本项控制的是**工具执行是否需要
// 用户确认**。参照 reasonix 的 ComposerChoice —— 说明文字**随选项走**（option.description），
// 而不是在控件旁外挂一行提示；所以这里把每一档的语义做成该选项的**悬浮 pop 词**。
const TOOLS_MODE_META = [
  { value: 'ask', label: tr('询问'), desc: tr('写类工具需确认：shell 执行、文件写入、git 写操作、浏览器/网络访问') },
  { value: 'auto', label: tr('自动'), desc: tr('仅危险工具需确认（默认）：shell/命令执行等对外部世界的动作') },
  { value: 'yolo', label: tr('全自动'), desc: tr('跳过全部确认：所有工具直接执行（该操作会留审计日志）') },
] as const

/** 选项 label 用 VNode 包一层 Tooltip —— 悬浮即见该档语义（对齐 reasonix 的 option.description） */
const toolsModeOptions = computed(() =>
  TOOLS_MODE_META.map(o => ({
    value: o.value,
    label: h(Tooltip, { title: o.desc, placement: 'left' }, () => o.label),
  })),
)

function onToolsModeChange(v: any) {
  emit('tools-mode-change', String(v))
}

// ── 输入框 placeholder 多态 ──
//
// 对齐 reasonix 的 `composerPlaceholder`：placeholder 不是一句固定文案，而是**随状态**
// 告诉用户"此刻该做什么"。原先只有 dragOver 一种，于是生成中仍写着"发送消息…"，
// 而实际上此时 Enter 是"打断并发送"——语义相反，容易误操作。
const inputPlaceholder = computed(() => {
  if (dragOver.value) return tr('松开以上传文件')
  if (props.loading) return tr('正在生成…按 Enter 打断并发送')
  if (props.disabled) return tr('当前会话不可输入')
  return tr('发送消息…（/ 查看命令 · ↑ 召回历史）')
})

// ── 运行态内联 ──
//
// 对齐 reasonix 的 `composer-intent-menu__goal-runtime-line`：让"它确实在动"可见。
// 长任务里"只有按钮变成停止"太弱 —— 用户会怀疑卡死，然后去点停止（正是子 Agent
// 被级联取消的常见触发路径）。一个走秒的计时器是最低成本的"活性"证据。
const elapsed = ref(0)
let elapsedTimer: ReturnType<typeof setInterval> | undefined

watch(
  () => props.loading,
  (v) => {
    if (elapsedTimer !== undefined) {
      clearInterval(elapsedTimer)
      elapsedTimer = undefined
    }
    if (v) {
      elapsed.value = 0
      elapsedTimer = setInterval(() => { elapsed.value += 1 }, 1000)
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  if (elapsedTimer !== undefined) clearInterval(elapsedTimer)
})

// 会话切换/恢复时同步（父组件回传 llm_config.model；空 = 后端默认）
watch(() => props.model, (v) => { modelValue.value = v || '' }, { immediate: true })

// 挂载自动聚焦 + 加载可用模型
onMounted(async () => {
  textareaRef.value?.focus?.()
  try {
    modelsLoading.value = true
    models.value = await listModels()
    // 模型默认兜底：列表到达时若还没有选中任何模型，**自动选第一个可用模型**
    // 并把选择上报给父组件。
    // 否则新建 / 刷新会话时选择器会一直是"未选中"状态（`props.model` 初始为空，
    // 而那个 watch 只在父组件回传时才同步），提交时也不带 model →
    // 落到后端默认模型 → 一旦默认模型被上游下架，就报
    // "Upstream request failed: Model is unavailable."（见 logs/python-engine.stderr.log）。
    if (!modelValue.value && models.value.length) {
      modelValue.value = models.value[0].name
      emit('model-change', modelValue.value)
    }
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
//
// **但必须按账号隔离**：localStorage 是浏览器级的，不带账号维度时同一浏览器换账号后
// `↑` 会召回**上一个账号**发过的内容（实测越权）。键经 privateKey 加账号命名空间，
// 登出时由 clearPrivateStorage 清理（见 utils/privateStorage）。
const HISTORY_KEY_BASE = 'chiron:composer-history:v1'
const HISTORY_LIMIT = 50
const auth = useAuthStore()
const historyKey = computed(() => privateKey(HISTORY_KEY_BASE, auth.user?.id))
const history = ref<string[]>([])
/** 召回游标：null = 未在召回（显示自己写的草稿） */
const historyCursor = ref<number | null>(null)
/** 首次召回前的草稿，用于「越过最近一条」时回到自己写的内容 */
let draftBeforeRecall = ''

function readHistory(): string[] {
  const raw = readLocal(historyKey.value)
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : []
  } catch {
    return []
  }
}

/** 载入当前账号的历史；账号变化（登录 / 切换）时由 watch 重新载入 */
function loadHistory() {
  history.value = readHistory()
  historyCursor.value = null
}

onMounted(loadHistory)
watch(historyKey, loadHistory)

function pushHistory(text: string) {
  // 重复提问移到最近，而不是堆第二份
  const next = history.value.filter(v => v !== text)
  next.push(text)
  history.value = next.slice(-HISTORY_LIMIT)
  writeLocal(historyKey.value, JSON.stringify(history.value))
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

// ── `@` 提及：把工作台资源（知识库 / Agent / 技能 / 工作流 / 插件）直接带进对话 ──
//
// 与 `/` 命令共用同一套菜单交互（上下键 / Enter / Esc / hover）；选中的资源以
// **引用 chip** 交给父组件，与 URL chips 走同一条链路（最终进 runtime.context）。
// 此前这些资源只能在首页或手写 URL query 才能带进对话 —— 输入框里没法选。
const mentionOpen = ref(false)
const mentionQuery = ref('')
const mentionIndex = ref(0)
const mentionLoading = ref(false)
const mentionItems = ref<{ type: string; id: string; name: string }[]>([])

/** `@` 提及的类型标签；写成函数以便每次渲染取当前语言（模块级 `tr` 不具响应式）。 */
function mentionLabel(type: string): string {
  const map: Record<string, string> = {
    kb: tr('知识库'), agent: 'Agent', skill: tr('技能'), workflow: tr('工作流'), plugin: tr('插件'),
  }
  return map[type] || type
}

const filteredMentions = computed(() => {
  const q = mentionQuery.value.trim().toLowerCase()
  const list = q
    ? mentionItems.value.filter(m => `${m.name} ${m.id}`.toLowerCase().includes(q))
    : mentionItems.value
  return list.slice(0, 20)
})

/** 分类的固定展示顺序（不按数量抖动 —— 用户对"知识库在最上"有稳定预期） */
const MENTION_TYPE_ORDER = ['kb', 'agent', 'skill', 'workflow', 'plugin']

/**
 * 按资源类型分组。
 *
 * 平铺列表在资源一多就分辨不出"这条是知识库还是技能"；分组后每类一个标题。
 * 每个条目**带着它在扁平列表里的下标**（`flatIndex`）—— 键盘导航与 hover 仍然用
 * 同一个 `mentionIndex` 整数，不必改成二维坐标（改坐标会牵连输入法/键盘逻辑）。
 */
const groupedMentions = computed(() => {
  const flat = filteredMentions.value
  const byType = new Map<string, { item: (typeof flat)[number]; flatIndex: number }[]>()
  flat.forEach((item, flatIndex) => {
    const list = byType.get(item.type) ?? []
    list.push({ item, flatIndex })
    byType.set(item.type, list)
  })
  const ordered = [
    ...MENTION_TYPE_ORDER,
    ...[...byType.keys()].filter(t => !MENTION_TYPE_ORDER.includes(t)),
  ]
  return ordered
    .filter(type => byType.has(type))
    .map(type => ({ type, label: mentionLabel(type), entries: byType.get(type)! }))
})

/** 懒加载 + 逐类容错：任一资源类失败只让那一类为空，不拖垮整个面板 */
async function loadMentionItems() {
  if (mentionItems.value.length || mentionLoading.value) return
  mentionLoading.value = true
  try {
    const api = await import('../../api')
    const toItems = async (loading: Promise<unknown>, type: string) => {
      try {
        const list = (await loading) as Array<{ id?: string; name?: string }> | undefined
        return (list || []).map(x => ({ type, id: String(x?.id ?? ''), name: String(x?.name ?? x?.id ?? '') }))
          .filter(x => x.id)
      } catch {
        return []
      }
    }
    const groups = await Promise.all([
      toItems(api.listKnowledgeBases(), 'kb'),
      toItems(api.listAgents(), 'agent'),
      toItems(api.listSkillResources(), 'skill'),
      toItems(api.listWorkflows(), 'workflow'),
      toItems(api.listPlugins(), 'plugin'),
    ])
    mentionItems.value = groups.flat()
  } finally {
    mentionLoading.value = false
  }
}

/** 在 input 事件里判定是否处于 `@` 查询中（词中 @ 不触发，避免邮箱之类误开面板） */
function syncMention() {
  const caret = textareaRef.value?.selectionStart ?? input.value.length
  const before = input.value.slice(0, caret)
  const at = before.lastIndexOf('@')
  if (at < 0) {
    mentionOpen.value = false
    return
  }
  const prev = at > 0 ? before[at - 1] : ' '        // 行首可视作空白
  const query = before.slice(at + 1)
  if (!/\s/.test(prev) || /\s/.test(query)) {
    mentionOpen.value = false
    return
  }
  mentionQuery.value = query
  mentionIndex.value = 0
  mentionOpen.value = true
  void loadMentionItems()
}

/** 选中一项：把 `@query` 从文本里摘掉，引用交给父组件（chips 体系） */
function pickMention(item: { type: string; id: string; name: string }) {
  const caret = textareaRef.value?.selectionStart ?? input.value.length
  const before = input.value.slice(0, caret)
  const at = before.lastIndexOf('@')
  if (at >= 0) input.value = input.value.slice(0, at) + input.value.slice(caret)
  mentionOpen.value = false
  saveDraft()
  emit('mention-add', { type: item.type, id: item.id, name: item.name })
  nextTick(() => textareaRef.value?.focus?.())
}

/**
 * 输入事件的**唯一入口**：`@` 提及与 `/` 命令都在这里同步。
 * （一个元素上写两个 `@input` 会产生重复属性 —— TS1117，所以必须收口成一个 handler。）
 */
function onComposerInput() {
  syncMention()
  onSlashInput()
}

function onKeydown(e: KeyboardEvent) {
  // `@` 提及面板导航（先判它：与斜杠菜单不会同时开，但先判更安全）
  if (mentionOpen.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      mentionIndex.value = Math.min(mentionIndex.value + 1, filteredMentions.value.length - 1)
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      mentionIndex.value = Math.max(mentionIndex.value - 1, 0)
      return
    }
    if (e.key === 'Escape') {
      mentionOpen.value = false
      return
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      const item = filteredMentions.value[mentionIndex.value]
      if (item) {
        e.preventDefault()
        pickMention(item)
        return
      }
    }
  }
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
  if (!text && !pendingAttachments.value.length) return
  const atts = pendingAttachments.value.length ? [...pendingAttachments.value] : undefined
  if (text) pushHistory(text)
  historyCursor.value = null
  input.value = ''
  pendingAttachments.value = []
  // 发送后清空草稿
  saveDraft()
  // 生成中提交 = **打断并发送**（ZCode 的 composer 状态机里，`running + 有草稿` 不是"只能等"）。
  // 分工：点按钮仍是"停止"（语义明确、防误触），按 Enter 才是"打断并发送"。
  // 已知限制：stop 目前只断开前端 SSE，引擎侧仍在跑（后端没有取消接口）——
  // 这与"停止"按钮的既有行为一致，这里不引入新的不一致。
  if (props.loading) emit('stop')
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
        message.error(tr('{name} 超过 50MB 限制', { name: file.name }))
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
    message.error(tr('文件上传失败: {error}', { error: e.message || tr('网络错误') }))
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

// ── 语音输入（Speech-to-Text，纯前端） ──
//
// 用浏览器原生 Web Speech API 在**浏览器侧**完成识别，文字直接落进输入框：
// 不生成音频文件、不经后端、不调用 Whisper。原实现是 MediaRecorder 录成 webm →
// 上传 → 后端 speech_to_text 调 Whisper API，既有 API 费用又占后端资源；
// 而多数场景用户要的只是"把说的话变成字"。
const SpeechRec =
  (window as unknown as { SpeechRecognition?: any; webkitSpeechRecognition?: any }).SpeechRecognition
  || (window as unknown as { webkitSpeechRecognition?: any }).webkitSpeechRecognition
const speechSupported = !!SpeechRec

const recording = ref(false)
const recordingTimer = ref(0)
let recordingInterval: ReturnType<typeof setInterval> | null = null
let recognizer: any = null
/** 本次会话已定稿的文本（interim 结果每次重发整段，需与它拼接） */
let speechFinal = ''
/** 开始转写前的输入框内容：识别结果追加在它之后 */
let speechPrefix = ''

function startRecording() {
  if (!speechSupported) {
    message.warning(tr('当前浏览器不支持语音转写，请使用 Chrome / Edge / Safari'))
    return
  }
  try {
    recognizer = new SpeechRec()
    recognizer.lang = 'zh-CN'
    recognizer.continuous = true
    recognizer.interimResults = true

    speechFinal = ''
    speechPrefix = input.value ? input.value.replace(/\s*$/, ' ') : ''

    recognizer.onresult = (e: any) => {
      let interim = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i]
        if (r.isFinal) speechFinal += r[0].transcript
        else interim += r[0].transcript
      }
      // 定稿 + 临时结果实时上屏：用户随时能看到识别到哪了
      input.value = speechPrefix + speechFinal + interim
    }
    recognizer.onerror = (e: any) => {
      // no-speech / aborted 属正常收尾（静音超时、用户停止），不当错误
      if (e?.error === 'no-speech' || e?.error === 'aborted') return
      message.error(
        e?.error === 'not-allowed'
          ? tr('麦克风权限被拒绝，请在浏览器设置中允许后重试')
          : `${tr('语音转写失败')}: ${e?.error || ''}`,
      )
      stopRecording()
    }
    // 浏览器可能自行结束（静音超时）；统一收尾，避免状态卡在"录音中"
    recognizer.onend = () => { if (recording.value) stopRecording() }

    recognizer.start()
    recording.value = true
    recordingTimer.value = 0
    recordingInterval = setInterval(() => { recordingTimer.value++ }, 1000)
  } catch {
    message.error(tr('无法启动语音转写，请检查麦克风权限'))
  }
}

function stopRecording() {
  if (recognizer) {
    // 先置空再 stop：onend 会回调进来，置空可避免递归
    const r = recognizer
    recognizer = null
    try { r.stop() } catch { /* 已停止 */ }
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
// 用 computed 而非常量数组：desc 是用户可见文案，常量在模块加载时求值一次，
// 语言切换后会一直停在旧语言。
const SLASH_COMMANDS = computed(() => [
  { cmd: '/clear', desc: tr('清空当前对话') },
  { cmd: '/export', desc: tr('导出当前会话为 Markdown') },
  { cmd: '/new', desc: tr('新建会话') },
  { cmd: '/theme', desc: tr('切换暗色/亮色模式') },
  { cmd: '/stop', desc: tr('停止生成') },
])
const showSlashMenu = ref(false)
const slashIndex = ref(0)
const filteredCommands = computed(() => {
  const q = input.value.trim().toLowerCase()
  if (!q.startsWith('/')) return []
  return SLASH_COMMANDS.value.filter(c => c.cmd.startsWith(q))
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
          {{ $t('已粘贴 {n} 字符', { n: pastedLargeText.length }) }}
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
      <!-- `@` 提及面板：与斜杠命令同一套交互与样式，数据源换成可引用资源 -->
      <div
        v-if="mentionOpen"
        class="slash-menu mention-menu"
        role="listbox"
        :aria-label="$t('引用资源')"
      >
        <div
          v-if="mentionLoading"
          class="slash-item"
        >
          {{ $t('加载中…') }}
        </div>
        <div
          v-else-if="!filteredMentions.length"
          class="slash-item"
        >
          {{ $t('没有匹配的资源（知识库 / Agent / 技能 / 工作流 / 插件）') }}
        </div>
        <template v-else>
          <!-- 按资源类型分组：每类一个标题；条目仍用**扁平下标**驱动键盘与 hover，
               所以 input/keydown 那边的整数索引逻辑一行都不用改。 -->
          <template
            v-for="group in groupedMentions"
            :key="group.type"
          >
            <div class="slash-group">{{ $t(group.label) }}</div>
            <div
              v-for="entry in group.entries"
              :key="`${entry.item.type}:${entry.item.id}`"
              class="slash-item"
              :class="{ active: entry.flatIndex === mentionIndex }"
              role="option"
              :aria-selected="entry.flatIndex === mentionIndex"
              @mouseenter="mentionIndex = entry.flatIndex"
              @click="pickMention(entry.item)"
            >
              <span class="slash-desc">{{ entry.item.name }}</span>
            </div>
          </template>
        </template>
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
      <!-- 上下文 chip 行：已带入本次对话的知识库 / Agent / 技能 / 工作流 / 插件 / 记忆分类。
           对齐 reasonix 的 `composer-context` —— 绑定是"沉默生效"的，不显式呈现就是幽灵行为：
           用户选了 Agent 却发现"它没用自己的工具"，往往只是因为看不见到底带进去了什么。 -->
      <div
        v-if="contextChips?.length"
        class="context-chips"
        :aria-label="$t('本次对话的上下文')"
      >
        <span
          v-for="chip in contextChips"
          :key="`${chip.type}:${chip.value}`"
          class="context-chip"
        >
          <span class="context-chip__label">{{ chip.label }}</span>
          <button
            type="button"
            class="context-chip__x"
            :title="$t('移除')"
            :aria-label="`${$t('移除')} ${chip.label}`"
            @click="emit('remove-context-chip', chip)"
          >
            <CloseOutlined />
          </button>
        </span>
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
        :auto-size="{ minRows: 1, maxRows: 8 }"
        :placeholder="inputPlaceholder"
        class="input-field"
        :disabled="disabled"
        :aria-label="$t('消息输入框')"
        @keydown="onKeydown"
        @input="onComposerInput"
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
            :title="recording
              ? $t('点击停止语音输入')
              : (speechSupported ? $t('语音输入（浏览器本地转写，不上传音频）') : $t('当前浏览器不支持语音转写'))"
            @click="recording ? stopRecording() : startRecording()"
          >
            <template #icon>
              <AudioOutlined />
            </template>
            <span v-if="recording" class="recording-timer">{{ formatRecordingTime(recordingTimer) }}</span>
          </Button>
          <!-- 模式 + 工具授权收进一个 popover：两者都是"偶发调整"的设置项，
               常驻会让底栏拥挤且与模型抢注意力；模型是高频切换项，仍留在底栏。
               （工具授权的档位语义仍是**选项悬浮 pop 词**，见 toolsModeOptions。） -->
          <Popover
            trigger="click"
            placement="topLeft"
            :title="$t('对话设置')"
          >
            <template #content>
              <div class="settings-pop">
                <label class="settings-pop__row">
                  <span class="settings-pop__label">{{ $t('模式') }}</span>
                  <Select
                    :model-value="mode"
                    :options="modeOptions"
                    size="small"
                    style="width: 150px"
                    @update:value="(v: any) => emit('update:mode', String(v))"
                  />
                </label>
                <label class="settings-pop__row">
                  <span class="settings-pop__label">{{ $t('工具授权') }}</span>
                  <Select
                    :model-value="toolsMode || 'auto'"
                    :options="toolsModeOptions"
                    size="small"
                    style="width: 150px"
                    @update:value="onToolsModeChange"
                  />
                </label>
              </div>
            </template>
            <Button
              type="text"
              size="small"
              class="context-btn settings-btn"
              :title="$t('对话设置：模式 / 工具授权')"
            >
              <template #icon>
                <SettingOutlined />
              </template>
              <span class="context-label">{{ $t('设置') }}</span>
            </Button>
          </Popover>
          <span class="input-divider" />
          <span class="mode-label">{{ $t('模型') }}</span>
          <Select
            class="model-select"
            :model-value="modelValue"
            :options="modelOptions"
            :loading="modelsLoading"
            size="small"
            allow-clear
            :placeholder="$t('默认模型')"
            :title="$t('chat.input.modelTitle', { model: modelValue || $t('chat.input.modelDefault') })"
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
          <span v-if="loading" class="run-hint">
            <span class="run-dot" />
            {{ $t('生成中') }} · {{ elapsed }}s
          </span>
          <span class="input-hint">{{ $t('Enter 发送 · Shift+Enter 换行') }}</span>
          <Button
            class="send-btn"
            :type="loading ? 'default' : 'primary'"
            :class="{ 'send-btn--stop': loading }"
            :disabled="(!input.trim() && !pendingAttachments.length && !loading) || disabled"
            :title="loading ? $t('chat.input.stopSend') : $t('chat.input.send')"
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
/* ══ 对话输入区 ══
   设计要点（对齐 reasonix 的 composer）：
   ① 卡片化：更大圆角 + 多层柔和阴影，聚焦时主色描边 + 3px 光晕；
   ② 分区：输入区与操作区之间一条分隔线，视觉上不再"一堆控件挤一行"；
   ③ 控件规格统一：32px 触控目标 + 8px 圆角，不再用 size=small 的"小气"感；
   ④ 响应式：container query（卡片自身宽度）而非只靠视口断点。 */
.input-area { padding: 0 16px 12px; }
.input-card {
  container-type: inline-size;
  position: relative;
  display: flex; flex-direction: column; gap: 2px;
  width: 100%; max-width: var(--chat-content-width); margin: 0 auto;
  padding: 12px 14px 10px;
  border: 1px solid var(--border); border-radius: 16px;
  background: var(--bg-input);
  box-shadow: var(--shadow-md);
  font-size: 16px; line-height: 24px;
  transition: border-color var(--dur-fast) ease, box-shadow var(--dur-fast) ease;
}
/* 聚焦态：主色描边 + 柔和光晕（用 color-mix 让深浅色主题都自然过渡） */
.input-card:focus-within {
  border-color: color-mix(in srgb, var(--primary) 45%, var(--border));
  box-shadow:
    0 0 0 3px color-mix(in srgb, var(--primary) 14%, transparent),
    var(--shadow-md);
}
.input-field { background: transparent !important; }
.input-field :deep(textarea) { color: var(--text-primary) !important; font-size: 16px !important; line-height: 24px !important; }
/* 操作区：与输入区之间一条分隔线（reasonix 的分区思路），不再与文字抢同一块空间 */
.input-actions {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; flex-wrap: wrap;
  padding-top: 8px; margin-top: 6px;
  border-top: 1px solid var(--border-soft, var(--border));
}
.input-left { display: flex; align-items: center; gap: 2px; }
/* 竖分隔线：把"工具按钮组"与"模型下拉"在视觉上分开，避免一堆控件糊成一片 */
.input-divider {
  flex: none; width: 1px; height: 18px; margin: 0 6px;
  background: var(--border-soft, var(--border));
}
@container (max-width: 620px) { .input-divider { display: none; } }
.input-hint { font-size: 12px; color: var(--text-tertiary); }
/* 工具栏控件统一规格：32px 触控目标 + 8px 圆角（覆盖 antd 的 size=small 观感） */
.input-actions .ant-btn:not(.send-btn) { height: 32px; min-width: 32px; border-radius: 8px; }
.input-actions .ant-select-single .ant-select-selector { border-radius: 8px; }
/* 运行态内联：生成中的活性证据（走秒），减少"以为卡死→点停止"的误操作 */
.run-hint {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.run-dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--primary, #1677ff);
  animation: run-pulse var(--dur-pulse) ease-in-out infinite;
}
@keyframes run-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
/* 模式选择器标签 */
.mode-label { flex: none; font-size: 12px; color: var(--text-tertiary); }
/* 模型路由下拉（与模式切换器同排；窄屏收缩防溢出） */
.model-select { width: 170px; }
.model-select :deep(.ant-select-selector) { font-size: 12px; }
/* 上下文快捷按钮：展开侧栏（抽屉模式） */
.context-btn { color: var(--text-tertiary); display: inline-flex; align-items: center; gap: 4px; border-radius: 8px; }
.context-btn:hover { color: var(--primary) !important; background: var(--primary-bg) !important; }
/* 对话设置 popover（模式 / 工具授权）：垂直两行，label 右对齐，与底栏解耦 */
.settings-pop { display: flex; flex-direction: column; gap: 10px; min-width: 230px; }
.settings-pop__row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.settings-pop__label { font-size: 12px; color: var(--text-secondary); flex: none; }
.context-label { font-size: 12px; }
/* 发送按钮：36px 圆形，可发送时主色 + hover 微放大 */
.send-btn.ant-btn { width: 36px; height: 36px; min-width: 36px; }
.send-btn { transition: transform var(--dur-fast) ease, box-shadow var(--dur-fast) ease, opacity var(--dur-fast) ease; }
.send-btn:not(:disabled):hover { transform: scale(1.06); box-shadow: 0 4px 12px var(--primary-bg); filter: brightness(1.05); }
.send-btn:disabled { opacity: 0.45; }
/* 生成中：圆形 → 方形，让"现在能停"一眼可辨。
   antd-vue 的 ButtonShape 只有 circle/round（不含 square），故用圆角覆盖实现；
   同时取消"放大 + 加深"的 hover —— 那是"发送"的暗示，不该出现在"停止"上。 */
.send-btn--stop { border-radius: var(--radius-lg) !important; }
.send-btn--stop:not(:disabled):hover { transform: none; box-shadow: none; filter: none; }

/* ── 响应式：以**卡片自身宽度**为准（container query），比视口断点更准确 ── */
@container (max-width: 560px) {
  .input-hint { display: none; }              /* 窄屏隐藏提示文字，占位符承担语义 */
  .model-select { width: 140px; }
}
@container (max-width: 420px) {
  .mode-label { display: none; }              /* 极窄：标签去掉，只留下拉本体 */
  .model-select { width: 118px; }
}
@media (max-width: 768px) {
  .input-area { padding: 0 12px calc(12px + env(safe-area-inset-bottom)); }
  .input-card { border-radius: 14px; padding: 10px 12px 8px; }
  .input-actions { gap: 6px; row-gap: 4px; }
}
/* ── 移动端：贴底 + 安全区 + 触控目标放大到 40px（拇指友好） ── */
@media (max-width: 576px) {
  .input-area { padding: 0 8px calc(10px + env(safe-area-inset-bottom)); }
  .input-card { border-radius: 12px; padding: 10px 10px 8px; }
  .context-label { display: none; }
  .input-actions .ant-btn:not(.send-btn) { height: 40px; min-width: 40px; border-radius: 10px; }
  .send-btn.ant-btn { width: 40px; height: 40px; min-width: 40px; }
  .input-left:last-child { margin-left: auto; }   /* 发送组靠右，避免与左侧工具组抢行 */
  .paste-btn { min-height: 36px; }
}

/* 附件预览区 */
.attachment-preview { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 0 8px; }
/* 上下文 chip 行：把"沉默生效"的绑定显式呈现（对齐 reasonix 的 composer-context） */
.context-chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 2px 0 8px; }
.context-chip {
  display: inline-flex; align-items: center; gap: 4px; max-width: 220px;
  padding: 2px 4px 2px 8px; border: 1px solid var(--border); border-radius: 999px;
  background: var(--bg-hover, rgba(127, 127, 127, 0.08));
  font-size: 12px; color: var(--text-secondary);
}
.context-chip__label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.context-chip__x {
  display: inline-flex; align-items: center; justify-content: center;
  border: none; background: transparent; cursor: pointer; line-height: 1;
  color: var(--text-tertiary); font-size: 10px; padding: 0 2px;
}
.context-chip__x:hover { color: var(--danger, var(--error)); }
.att-thumb { position: relative; width: 64px; height: 64px; border-radius: var(--sig-radius-card); border: 1px solid var(--border); background: var(--bg-card); overflow: hidden; }
.att-thumb-img { width: 100%; height: 100%; object-fit: cover; }
.att-thumb-file { width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; padding: 4px; color: var(--text-tertiary); font-size: 10px; }
.att-thumb-name { max-width: 56px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.att-remove { position: absolute; top: 2px; right: 2px; width: 18px; height: 18px; border-radius: 50%; border: none; background: var(--bg-overlay); color: var(--on-solid); display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 10px; }
.att-remove:hover { background: var(--error); }
.attach-btn { color: var(--text-tertiary); display: inline-flex; align-items: center; justify-content: center; }
.attach-btn:hover { color: var(--primary); }
/* 录音按钮 */
.record-btn { color: var(--text-tertiary); display: inline-flex; align-items: center; justify-content: center; gap: 4px; }
.record-btn:hover { color: var(--primary); }
.record-btn.recording { color: var(--error); animation: pulse-rec var(--dur-pulse) ease-in-out infinite; }
@keyframes pulse-rec {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.recording-timer { font-size: 12px; font-variant-numeric: tabular-nums; min-width: 32px; }
/* 拖拽态：边框主色 + 背景淡色 */
.input-card.drag-active { border-color: var(--primary); background: var(--primary-bg); box-shadow: var(--shadow-md), 0 0 0 3px var(--primary-bg); }

/* 斜杠命令面板 */
.slash-menu { position: absolute; bottom: 100%; left: 0; right: 0; margin-bottom: 4px; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--sig-radius-card); box-shadow: var(--shadow-lg); overflow: hidden; z-index: var(--z-sticky); }
/* 分组标题：**粘在列表顶部**，滚动时始终看得出"当前在哪一类"
   （比整块滚动遮罩更实用：遮罩只提示"还有内容"，粘性标题直接回答"我在哪"）。 */
.slash-group {
  position: sticky; top: 0; z-index: var(--z-content);
  padding: 4px 12px;
  background: var(--bg-card);
  color: var(--text-tertiary); font-size: 11px; font-weight: 600;
  border-bottom: 1px solid var(--border);
}
.slash-item { display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; cursor: pointer; transition: background var(--dur-fast) ease; }
.slash-item.active { background: var(--bg-hover); }
.slash-cmd { font-weight: 600; color: var(--primary); font-size: 13px; }
.slash-desc { color: var(--text-tertiary); font-size: 12px; }

/* 大文本粘贴折叠预览 */
.paste-preview { border: 1px solid var(--border); border-radius: var(--sig-radius-card); padding: 8px 12px; background: var(--bg-secondary); margin-bottom: 4px; }
.paste-preview-text { font-size: 12px; color: var(--text-secondary); line-height: 1.5; max-height: 80px; overflow: hidden; white-space: pre-wrap; word-break: break-all; }
.paste-preview-meta { font-size: 11px; color: var(--text-tertiary); margin: 4px 0; }
.paste-preview-actions { display: flex; justify-content: flex-end; gap: 8px; }
.paste-btn { padding: 2px 10px; border-radius: var(--sig-radius-button); border: 1px solid var(--border); background: var(--bg-card); color: var(--text-secondary); font-size: 12px; cursor: pointer; }
.paste-btn.accept { background: var(--primary); color: var(--on-solid); border-color: var(--primary); }
.paste-btn.accept:hover { opacity: 0.9; }
.paste-btn.discard:hover { color: var(--error); border-color: var(--error); }
</style>