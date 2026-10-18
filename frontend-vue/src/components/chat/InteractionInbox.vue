<script setup lang="ts">
/**
 * 人工介入待办收件箱（M7c）。
 *
 * 它补的是**跨进程**这一段：某一轮跑到需要确认/回答时以 `awaiting_input` 收尾，现场落进
 * `pending_interactions`。实时那两张卡（ChatView 的审批区、AskCard）只在 SSE 还开着时出现；
 * 一旦刷新页面、切换会话或那一轮的进程已经没了，就只剩这张表还记着"欠用户一个答复"。
 * 这里就是那张表的读视图 + 答复入口。
 *
 * 与实时卡片的分工：两者可以并存（同一件事既在流里、也在库里），但**答复只认这一张票的 id**，
 * 写成功就是 `answered`。答复**不会自己把那一轮跑起来** —— 发 `wake` 让父层顺手提交一轮，
 * 引擎在下一轮开头从暂停处接手并消费这张票（见 python-engine/app/api/interactions.py 的说明）。
 *
 * 渲染复用两张已有卡片（`SubAgentApprovalCard` / `AskCard`）：同一件事在第二个地方出现时，
 * 不该让用户重新建立心智。这里只负责取列表、按库里的 `kind` 提交、把结果回填成已答复态。
 */
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import AskCard from './AskCard.vue'
import SubAgentApprovalCard from './SubAgentApprovalCard.vue'
import {
  listPendingInteractions,
  resolveInteraction,
  type PendingInteraction,
} from '../../api/interactions'
import { describeApiError } from '../../utils/apiError'

const props = defineProps<{ sessionId: string }>()
const emit = defineEmits<{ (e: 'wake'): void }>()

const { t } = useI18n()

const items = ref<PendingInteraction[]>([])
const loadingList = ref(false)
const submitting = ref<Record<string, boolean>>({})
const errors = ref<Record<string, string>>({})

async function refresh() {
  if (!props.sessionId) {
    items.value = []
    return
  }
  loadingList.value = true
  try {
    items.value = await listPendingInteractions(props.sessionId)
  } catch {
    // 拉不到就什么都不显示：这是"补漏"的入口，实时卡片才是主路径，不该因为它反过来打断对话
    items.value = []
  } finally {
    loadingList.value = false
  }
}

// 换会话即重取：列表按来电者身份过滤（后端不按会话猜），切走切回看到的都是各自的待办
watch(() => props.sessionId, () => { void refresh() }, { immediate: true })

defineExpose({ refresh })

/** 提交一张票。形状由 `kind` 决定，后端按库里的 kind 校验（不看客户端声称的是什么）。 */
async function respond(item: PendingInteraction, response: Record<string, unknown>) {
  if (submitting.value[item.id]) return
  submitting.value[item.id] = true
  errors.value[item.id] = ''
  try {
    const ok = await resolveInteraction({
      interactionId: item.id,
      sessionId: props.sessionId,
      response,
    })
    if (!ok) {
      errors.value[item.id] = t('errors.answer_submission_failed_please_retry')
      return
    }
    // 就地转成已答复态：不必重拉列表就能看到"这张已处理"，也不给重复作答留按钮
    item.status = 'answered'
    emit('wake')
  } catch (e) {
    // 业务失败是网关映射出来的状态码（404 不存在 / 409 已有下文 / 400 形状不对）：
    // 全局拦截器只管 401/≥500，这些 4xx 必须在这里本地呈现，否则用户点了没反应。
    errors.value[item.id] = describeApiError(e)
  } finally {
    submitting.value[item.id] = false
  }
}

function decideApproval(item: PendingInteraction, approved: boolean) {
  void respond(item, { decision: approved ? 'approve' : 'reject' })
}

/** 编辑后批准：整份替换参数（不是补丁），与实时审批卡片、引擎 C5 二次校验同一契约。 */
function editApproval(item: PendingInteraction, args: string) {
  void respond(item, { decision: 'edit', arguments: JSON.parse(args) })
}

// ── 展示辅助：把库里 request 的字段摊给两张卡片 ──
function approvalArgs(item: PendingInteraction): string {
  const args = item.request?.arguments
  if (args == null) return ''
  try {
    return JSON.stringify(args)
  } catch {
    return ''
  }
}
function approvalContent(item: PendingInteraction): string {
  return String(item.request?.question ?? item.reason ?? '')
}
function approvalToolName(item: PendingInteraction): string {
  return String(item.tool_name || item.request?.tool_name || '')
}
function questionText(item: PendingInteraction): string {
  return String(item.request?.question ?? '')
}
function questionOptions(item: PendingInteraction): string[] {
  const options = item.request?.options
  return Array.isArray(options) ? options.map(o => String(o)) : []
}
</script>

<template>
  <div
    v-if="items.length"
    class="interaction-inbox"
  >
    <div class="inbox-head">
      <span class="inbox-title">{{ $t('agent.pending_interactions_title') }}</span>
      <button
        class="inbox-refresh"
        type="button"
        :disabled="loadingList"
        :aria-label="$t('common.retry')"
        @click="refresh"
      >
        {{ $t('common.retry') }}
      </button>
    </div>

    <div
      v-for="item in items"
      :key="item.id"
      class="inbox-item"
    >
      <!-- 已答复：只留一行说明，不给按钮（重复答同一张票会造出一张将来能被同类调用取走的票） -->
      <div
        v-if="item.status === 'answered'"
        class="inbox-answered"
      >
        <span class="inbox-answered-mark">✓</span>
        <span>{{ $t('agent.interaction_answered_waiting') }}</span>
      </div>

      <template v-else-if="item.kind === 'approval'">
        <SubAgentApprovalCard
          :tool-name="approvalToolName(item)"
          :args="approvalArgs(item)"
          :content="approvalContent(item)"
          :submitting="!!submitting[item.id]"
          :error="errors[item.id]"
          @decide="(approved: boolean) => decideApproval(item, approved)"
          @edit="(args: string) => editApproval(item, args)"
        />
      </template>

      <AskCard
        v-else-if="item.kind === 'question'"
        :question="questionText(item)"
        :options="questionOptions(item)"
        :expired="!!submitting[item.id]"
        @answer="(value: string) => respond(item, { answer: value })"
        @decline="() => respond(item, { declined: true })"
      />

      <div
        v-if="errors[item.id] && item.kind === 'question'"
        class="inbox-error"
      >
        {{ errors[item.id] }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.interaction-inbox {
  margin: 0 0 8px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg-surface);
}
.inbox-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.inbox-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-bg);
  padding: 2px 10px;
  border-radius: 10px;
}
.inbox-refresh {
  margin-left: auto;
  border: 1px solid var(--border);
  border-radius: var(--sig-radius-button);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 18px;
  padding: 2px 10px;
  cursor: pointer;
}
.inbox-refresh:disabled { opacity: 0.5; cursor: default; }
.inbox-item { margin-bottom: 8px; }
.inbox-item:last-child { margin-bottom: 0; }
.inbox-answered {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px dashed var(--border);
  border-radius: 10px;
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 13px;
}
.inbox-answered-mark { color: var(--success, #16a34a); }
.inbox-error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--danger, var(--error));
}
</style>
