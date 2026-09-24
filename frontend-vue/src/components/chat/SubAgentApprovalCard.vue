<script setup lang="ts">
/**
 * 子 Agent 工具确认卡片（P3-后续）。
 *
 * 为什么需要它：子 Agent 默认 `tools_mode=auto`，而 `shell_exec` 这类工具在 auto 下
 * 就需要确认 —— 所以"子 Agent 请求批准"几乎每次调用命令类工具都会发生。此前它只以
 * 一行 notice 出现，用户**看得见却批不了**：子 Agent 空转到 300s 超时后才以
 * `approval timed out` 被拒。
 *
 * 视觉与交互刻意与主对话区的工具确认卡片（ChatView 的 `.approval-card`）保持一致：
 * 同一件事（批准/拒绝一次工具调用）在第二个地方出现时，不该让用户重新建立心智。
 *
 * 组件只负责"展示 + 抛出决定"：回传通道由父组件负责
 * （`POST /v1/agent/approval`，与主 Agent 的审批走完全同一条通道）。
 */
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  /** 工具名（批准前必须知道要执行什么） */
  toolName?: string
  /** 工具参数（原始 JSON 字符串） */
  args?: string
  /** 引擎给的说明（含风险提示） */
  content?: string
  /** 正在提交：按钮禁用，避免同一个调用被决定两次 */
  submitting?: boolean
  /** 本地已知结论（'approved' | 'denied'）：显示结果而不是按钮 */
  decision?: string
  /** 提交失败原因：保留按钮让用户重试 —— 不做假成功 */
  error?: string
}>(), {
  toolName: '',
  args: '',
  content: '',
  submitting: false,
  decision: '',
  error: '',
})

const emit = defineEmits<{ (e: 'decide', approved: boolean): void }>()

/** 参数美化：解析失败就原样显示（展示不该影响可用性）。 */
const prettyArgs = computed(() => {
  const raw = (props.args || '').trim()
  if (!raw) return ''
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
})
</script>

<template>
  <div class="approval-card">
    <div class="approval-head">
      <span class="approval-tag">{{ $t('子 Agent 待确认') }}</span>
      <span class="approval-name">{{ toolName || $t('工具调用') }}</span>
      <span class="approval-hint">{{ $t('请在 300 秒内决定，超时按拒绝处理') }}</span>
    </div>
    <div
      v-if="content"
      class="approval-desc"
    >
      {{ content }}
    </div>
    <pre
      v-if="prettyArgs"
      class="approval-args"
    >{{ prettyArgs }}</pre>
    <div class="approval-actions">
      <span
        v-if="decision"
        class="approval-done"
      >
        {{ decision === 'approved' ? $t('已允许') : $t('已拒绝') }}
      </span>
      <template v-else>
        <button
          type="button"
          class="approval-btn allow"
          :disabled="submitting"
          @click="emit('decide', true)"
        >
          {{ $t('允许') }}
        </button>
        <button
          type="button"
          class="approval-btn danger"
          :disabled="submitting"
          @click="emit('decide', false)"
        >
          {{ $t('拒绝') }}
        </button>
      </template>
    </div>
    <div
      v-if="error"
      class="approval-error"
    >
      {{ error }}
    </div>
  </div>
</template>

<style scoped>
.approval-card {
  background: var(--bg-card); border: 1px solid var(--border);
  border-left: 3px solid var(--primary); border-radius: 10px; padding: 8px 10px;
}
.approval-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.approval-tag {
  flex: none; font-size: 11px; color: var(--primary);
  background: var(--primary-bg); padding: 2px 8px; border-radius: 10px;
}
.approval-name { font-weight: 600; font-size: 13px; color: var(--text-primary); }
.approval-hint { margin-left: auto; font-size: 11px; color: var(--warning, #f59e0b); }
.approval-desc { font-size: 12px; color: var(--text-secondary, #595959); margin-bottom: 6px; }
.approval-args {
  font-family: var(--font-mono); font-size: 12px; color: var(--text-muted);
  background: var(--surface-2, rgba(127, 127, 127, 0.06));
  padding: 6px; border-radius: 6px; overflow: auto; max-height: 140px; margin: 0 0 8px;
}
.approval-actions { display: flex; align-items: center; gap: 8px; }
.approval-btn { border: none; border-radius: 8px; padding: 4px 14px; font-size: 13px; cursor: pointer; }
.approval-btn:disabled { opacity: 0.5; cursor: default; }
.approval-btn.allow { background: var(--primary); color: var(--on-solid); }
.approval-btn.allow:hover:not(:disabled) { opacity: 0.9; }
.approval-btn.danger { background: var(--bg-hover); color: var(--text-primary); }
.approval-btn.danger:hover:not(:disabled) {
  background: var(--danger-bg, rgba(239, 68, 68, 0.12)); color: var(--danger, #ef4444);
}
.approval-done { font-size: 12px; color: var(--success, #16a34a); }
.approval-error { margin-top: 6px; font-size: 12px; color: var(--danger, #ef4444); }
</style>
