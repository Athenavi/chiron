<template>
  <div class="agent-collab-panel">
    <div class="panel-header">
      <div class="header-title">
        <svg
          width="20"
          height="20"
          viewBox="0 0 20 20"
          fill="currentColor"
        >
          <path d="M10 2a3 3 0 00-3 3v3a3 3 0 006 0V5a3 3 0 00-3-3zM6 9a3 3 0 00-3 3v1a3 3 0 006 0v-1a3 3 0 00-3-3zM17 12a3 3 0 01-3 3v1a3 3 0 01-6 0v-1a3 3 0 01-3-3 4 4 0 018 0z" />
        </svg>
        {{ $t('Agent 协同执行') }}
      </div>
      <div class="header-actions">
        <button
          class="btn-collapse"
          @click="collapsePanel"
        >
          {{ $t('收起') }}
        </button>
      </div>
    </div>
    
    <div
      v-if="!isCollapsed"
      class="panel-body"
    >
      <!-- Agent 列表 -->
      <div class="agents-container">
        <div
          v-for="(agent, index) in agents"
          :key="agent.id"
          class="agent-card"
          :class="{ active: agent.isActive, completed: agent.status === 'completed' }"
          role="button"
          tabindex="0"
          :aria-expanded="showOutput[index]"
          @click="showOutput[index] = !showOutput[index]"
          @keydown.enter.prevent="showOutput[index] = !showOutput[index]"
          @keydown.space.prevent="showOutput[index] = !showOutput[index]"
        >
          <!-- Agent 头像 -->
          <div
            class="agent-avatar"
            :style="{ backgroundColor: agent.color }"
          >
            <span class="agent-icon">{{ agent.icon }}</span>
            <div
              v-if="agent.status === 'running'"
              class="status-ring"
            />
          </div>
          
          <!-- Agent 信息 -->
          <div class="agent-info">
            <div class="agent-name">
              {{ agent.name }}
            </div>
            <div class="agent-role">
              {{ agent.role }}
            </div>
            <div class="agent-status">
              {{ statusText(agent.status) }}
            </div>
          </div>
          
          <!-- 执行状态 -->
          <div class="agent-actions">
            <div
              v-if="agent.status === 'running'"
              class="spinner-sm"
            />
            <div
              v-else-if="agent.status === 'completed'"
              class="check-icon"
            >
              ✓
            </div>
            <div
              v-else-if="agent.status === 'error'"
              class="error-icon"
            >
              ✗
            </div>
          </div>
          
          <!-- 输出预览 (点击展开) -->
          <div
            v-if="agent.output && showOutput[index]"
            class="agent-output"
          >
            <pre>{{ truncateOutput(agent.output) }}</pre>
          </div>
        </div>
      </div>
      
      <!-- 执行日志 -->
      <div class="execution-log">
        <div class="log-header">
          <span>{{ $t('执行日志') }}</span>
          <button
            class="btn-clear"
            @click="clearLog"
          >
            {{ $t('清空') }}
          </button>
        </div>
        <div
          ref="logContainer"
          class="log-content"
        >
          <div
            v-for="(log, index) in logs"
            :key="index"
            class="log-entry"
            :class="log.level"
          >
            <span class="log-time">{{ formatTime(log.timestamp) }}</span>
            <span class="log-agent">{{ log.agent }}</span>
            <span class="log-message">{{ log.message }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'

export interface AgentInfo {
  id: string
  name: string
  role: string
  icon: string
  color: string
  status: 'pending' | 'running' | 'completed' | 'error'
  output?: string
  isActive?: boolean
}

const props = defineProps<{
  agents: AgentInfo[]
  logs?: Array<{
    level: 'info' | 'warn' | 'error'
    agent: string
    message: string
    timestamp: number
  }>
}>()

const emit = defineEmits<{
  collapse: []
  /** 请求父组件清空日志数据（logs 是 prop，单向数据流由父组件管理） */
  clearLogs: []
}>()

const isCollapsed = ref(false)
const showOutput = ref<boolean[]>([])
const logContainer = ref<HTMLDivElement>()

function collapsePanel() {
  isCollapsed.value = true
  emit('collapse')
}

function statusText(status: string): string {
  const map: Record<string, string> = {
    pending: '等待中',
    running: '执行中',
    completed: '已完成',
    error: '失败',
  }
  return map[status] || status
}

function truncateOutput(output: string): string {
  if (!output) return ''
  return output.substring(0, 200) + (output.length > 200 ? '...' : '')
}

function clearLog() {
  emit('clearLogs')
}

function formatTime(timestamp: number): string {
  const date = new Date(timestamp)
  return date.toLocaleTimeString('zh-CN', { hour12: false })
}

// 滚动到底部
async function scrollToBottom() {
  await nextTick()
  if (logContainer.value) {
    logContainer.value.scrollTop = logContainer.value.scrollHeight
  }
}

onMounted(() => {
  // 初始化 showOutput
  showOutput.value = props.agents.map(() => false)
})

// 暴露方法供父组件调用
defineExpose({
  scrollToBottom,
})
</script>

<style scoped>
.agent-collab-panel {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
}

.header-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.header-actions {
  display: flex;
  gap: 8px;
}

.btn-collapse {
  min-height: 34px;
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.btn-collapse:hover {
  border-color: var(--primary);
  color: var(--primary);
}

.panel-body {
  max-height: 600px;
  overflow-y: auto;
}

.agents-container {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.agent-card {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-card);
  cursor: pointer;
  transition: border-color 0.2s ease, box-shadow 0.2s ease, background 0.2s ease;
}

.agent-card:hover {
  border-color: var(--primary);
}

.agent-card.active {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--primary-bg);
}

.agent-card.completed {
  border-color: var(--success);
}

.agent-avatar {
  position: relative;
  width: 40px;
  height: 40px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  flex-shrink: 0;
}

.agent-icon {
  font-size: 20px;
}

.status-ring {
  position: absolute;
  inset: -3px;
  border: 2px solid transparent;
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.agent-info {
  flex: 1;
  min-width: 0;
}

.agent-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 2px;
}

.agent-role {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}

.agent-status {
  font-size: 11px;
  color: var(--text-muted);
}

.agent-actions {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.spinner-sm {
  width: 16px;
  height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.check-icon {
  color: var(--success);
  font-size: 18px;
  font-weight: bold;
}

.error-icon {
  color: var(--error);
  font-size: 18px;
  font-weight: bold;
}

.agent-output {
  grid-column: 1 / -1;
  padding: 8px;
  background: var(--bg-secondary);
  border-radius: 6px;
  margin-top: 8px;
}

.agent-output pre {
  margin: 0;
  padding: 8px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 100px;
  overflow-y: auto;
}

.execution-log {
  border-top: 1px solid var(--border);
}

.log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
}

.log-header span {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.btn-clear {
  min-height: 30px;
  padding: 4px 10px;
  border: none;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
}

.btn-clear:hover {
  color: var(--error);
  background: var(--bg-hover);
}

.log-content {
  padding: 8px 16px;
  max-height: 200px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 12px;
}

.log-entry {
  display: flex;
  gap: 8px;
  padding: 4px 0;
  line-height: 1.5;
}

.log-entry.info {
  color: var(--text-primary);
}

.log-entry.warn {
  color: var(--warning);
}

.log-entry.error {
  color: var(--error);
}

.log-time {
  color: var(--text-muted);
  flex-shrink: 0;
}

.log-agent {
  color: var(--primary);
  flex-shrink: 0;
  min-width: 60px;
}

.log-message {
  flex: 1;
  white-space: pre-wrap;
  word-break: break-all;
}

@media (max-width: 768px) {
  .agent-card {
    flex-wrap: wrap;
  }

  .agent-info {
    flex-basis: calc(100% - 52px);
  }

  .agent-output {
    flex-basis: 100%;
  }

  .panel-body {
    max-height: none;
  }
}

@media (max-width: 576px) {
  .agents-container {
    padding: 12px;
  }

  .log-entry {
    flex-wrap: wrap;
    row-gap: 2px;
  }

  .log-time {
    width: 100%;
  }

  .log-agent {
    min-width: 48px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .btn-collapse,
  .btn-clear,
  .agent-card {
    transition: none;
  }
}
</style>
