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
 *
 * M15c 起多了第三类 `budget`（任务线预算耗尽 ⇒ 线被暂停，请人决定提到多少）。它没有现成卡片
 * 可复用，形状也根本不同：答复即刻改写那条线的上限并解除暂停，票当场关闭（不停在 `answered`
 * 等下一轮来取），而且**没有"拒绝"这一项** —— 不答复与拒绝提额在效果上是同一件事。
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
/** 提额输入框的草稿（按票 id 存）。预填的是字符串，而 `type=number` 的 v-model 回填的是
 *  **数字**（Vue 对 number 输入框做了 castToNumber），所以读取时统一 `String()` 再校验 ——
 *  当字符串直接 `.trim()` 会在用户改过一次输入框后抛 TypeError。 */
const capDraft = ref<Record<string, string | number>>({})

async function refresh() {
  if (!props.sessionId) {
    items.value = []
    return
  }
  loadingList.value = true
  try {
    items.value = await listPendingInteractions(props.sessionId)
    // 预填建议值：那是引擎按当前上限算出来的起点，让用户有个数字可改，而不是面对空框猜量级
    for (const item of items.value) {
      if (item.kind === 'budget' && capDraft.value[item.id] === undefined) {
        capDraft.value[item.id] = String(suggestedCap(item))
      }
    }
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
    if (item.kind === 'budget') {
      // 提额是**即刻生效**的：那张票已被关掉（consumed），不会再出现在列表里。
      // 把它就地标成"已答复"反而会留着一行谎 —— 它不是等下一轮来取的票。
      // 留着草稿：后端关票失败时这行还在，让用户看得见自己刚提交了什么。
      void refresh()
    } else {
      // 就地转成已答复态：不必重拉列表就能看到"这张已处理"，也不给重复作答留按钮
      item.status = 'answered'
    }
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

// ── 提额卡：把"这条线停了"变成"决定提到多少" ──
function suggestedCap(item: PendingInteraction): number {
  const raw = Number(item.request?.suggested_cap_tokens ?? 0)
  return Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 0
}

/** request 里的 token 数。读不到账目时后端**不写**这些键（`null` 不是"账上是 0"），
 *  所以缺键与负数一律显示破折号；这里宁可少给一个数字，也不能给一个看着可信的 0 ——
 *  用户会照着它决定提到多少。 */
function tokenCount(item: PendingInteraction, key: string): string {
  const raw = item.request?.[key]
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw < 0) return '—'
  return raw.toLocaleString()
}

function raiseBudget(item: PendingInteraction) {
  const raw = String(capDraft.value[item.id] ?? '').trim()
  const cap = Number(raw)
  // 只收正整数：`-1` 在后端是"显式不限"的哨兵值，而"不限"只能由运维配置，不能由一张待办发出来
  if (!raw || !Number.isInteger(cap) || cap <= 0) {
    errors.value[item.id] = t('agent.budget_invalid_cap')
    return
  }
  errors.value[item.id] = ''
  void respond(item, { cap_tokens: cap })
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

      <!-- 提额：一个数字框 + 一个提交。故意没有"拒绝"—— 不答复就是不提额，线继续停着 -->
      <div
        v-else-if="item.kind === 'budget'"
        class="inbox-budget"
      >
        <p class="inbox-budget-question">
          {{ questionText(item) }}
        </p>
        <p class="inbox-budget-numbers">
          {{ $t('agent.budget_exhausted_summary', {
            spent: tokenCount(item, 'settled_tokens'),
            cap: tokenCount(item, 'cap_tokens'),
          }) }}
        </p>
        <label class="inbox-budget-field">
          <span>{{ $t('agent.budget_new_cap_label') }}</span>
          <input
            v-model="capDraft[item.id]"
            type="number"
            min="1"
            step="1"
            :disabled="!!submitting[item.id]"
          >
        </label>
        <button
          class="inbox-budget-submit"
          type="button"
          :disabled="!!submitting[item.id]"
          @click="raiseBudget(item)"
        >
          {{ $t('agent.budget_submit') }}
        </button>
        <div
          v-if="errors[item.id]"
          class="inbox-error"
        >
          {{ errors[item.id] }}
        </div>
      </div>

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
  font-weight: var(--fw-semibold);
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
.inbox-budget {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-card);
}
.inbox-budget-question {
  margin: 0 0 6px;
  font-size: 13px;
  color: var(--text-primary);
}
.inbox-budget-numbers {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.inbox-budget-field {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.inbox-budget-field input {
  width: 160px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 13px;
}
.inbox-budget-submit {
  border: 1px solid var(--accent);
  border-radius: var(--sig-radius-button);
  background: var(--accent);
  color: var(--accent-text, #fff);
  font-size: 12px;
  line-height: 18px;
  padding: 3px 12px;
  cursor: pointer;
}
.inbox-budget-submit:disabled { opacity: 0.5; cursor: default; }
.inbox-error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--danger, var(--error));
}
</style>
