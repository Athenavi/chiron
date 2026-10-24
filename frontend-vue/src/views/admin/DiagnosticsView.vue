<template>
  <div class="diagnostics-view">
    <div class="page-header">
      <div>
        <h2 class="page-title page-title-sm">
          {{ $t('admin.diagnostics') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.diagnostics_desc') }}
        </p>
      </div>
      <Button
        :loading="loading"
        @click="load"
      >
        <template #icon>
          <ReloadOutlined />
        </template>
        {{ $t('common.refresh') }}
      </Button>
    </div>

    <Alert
      v-if="report"
      class="summary"
      :type="summaryAlertType"
      show-icon
      :message="summaryMessage"
    />

    <Spin :spinning="loading">
      <div
        v-for="group in groups"
        :key="group.category"
        class="group"
      >
        <h3 class="group-title">
          {{ group.label }}
        </h3>
        <div
          v-for="c in group.checks"
          :key="c.id"
          class="check-row"
          :class="`check-row--${c.status}`"
        >
          <span class="check-status">{{ statusIcon(c.status) }}</span>
          <div class="check-body">
            <div class="check-head">
              <span class="check-title">{{ c.title }}</span>
              <Tag :color="statusColor(c.status)">
                {{ $t(`admin.diag_status_${c.status}`) }}
              </Tag>
            </div>
            <div class="check-detail">
              {{ c.detail }}
            </div>
            <!-- 补救建议只在"需要处理"时出现：ok 的行不需要噪音（与后端一致：ok 时为空）。 -->
            <div
              v-if="c.remediation"
              class="check-fix"
            >
              <BulbOutlined />
              <span>{{ c.remediation }}</span>
            </div>
          </div>
        </div>
      </div>

      <Empty
        v-if="report && !report.checks.length"
        :description="$t('admin.diagnostics_empty')"
      />
    </Spin>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Alert, Button, Empty, Spin, Tag } from 'ant-design-vue'
import { BulbOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { getDiagnostics, type DiagnosticCheck, type DiagnosticStatus, type DiagnosticsReport } from '../../api/admin'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const loading = ref(false)
const report = ref<DiagnosticsReport | null>(null)

const CATEGORY_LABEL: Record<string, string> = {
  dependency: 'admin.diag_category_dependency',
  config: 'admin.diag_category_config',
  capability: 'admin.diag_category_capability',
}

// 后端已按"依赖 → 配置 → 能力"排好序；这里只做分组，**不重排** ——
// 顺序本身是"先看链路通不通、再看配置对不对、最后看功能是否可用"的排障次序。
const groups = computed(() => {
  const out: { category: string; label: string; checks: DiagnosticCheck[] }[] = []
  for (const c of report.value?.checks ?? []) {
    let g = out.find(x => x.category === c.category)
    if (!g) {
      g = { category: c.category, label: t(CATEGORY_LABEL[c.category] || 'admin.diag_category_other'), checks: [] }
      out.push(g)
    }
    g.checks.push(c)
  }
  return out
})

const summaryAlertType = computed(() => {
  switch (report.value?.status) {
    case 'fail': return 'error' as const
    case 'warn': return 'warning' as const
    case 'skipped': return 'info' as const
    default: return 'success' as const
  }
})

const summaryMessage = computed(() => {
  const r = report.value
  if (!r) return ''
  const c = r.counts || {}
  return t('admin.diag_summary', {
    status: t(`admin.diag_status_${r.status}`),
    ok: c.ok ?? 0,
    warn: c.warn ?? 0,
    fail: c.fail ?? 0,
    skipped: c.skipped ?? 0,
  })
})

function statusIcon(s: DiagnosticStatus): string {
  switch (s) {
    case 'ok': return '✓'
    case 'warn': return '!'
    case 'fail': return '✕'
    default: return '–'
  }
}

function statusColor(s: DiagnosticStatus): string {
  switch (s) {
    case 'ok': return 'success'
    case 'warn': return 'warning'
    case 'fail': return 'error'
    default: return 'default'
  }
}

async function load() {
  loading.value = true
  try {
    report.value = await getDiagnostics()
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.diagnostics-view { padding: 4px; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-secondary); }
.summary { margin-bottom: 16px; }
.group { margin-bottom: 18px; }
.group-title { font-size: 13px; font-weight: var(--fw-semibold); margin: 0 0 8px; color: var(--text-secondary); }
.check-row { display: flex; gap: 10px; padding: 10px 12px; border: var(--hairline-width) solid var(--border); border-radius: var(--radius-lg); margin-bottom: 8px; background: var(--bg-card); }
.check-status { flex: 0 0 auto; width: 18px; text-align: center; font-weight: var(--fw-bold); line-height: 20px; }
.check-row--ok .check-status { color: var(--success); }
.check-row--warn .check-status { color: var(--warning); }
.check-row--fail .check-status { color: var(--error); }
.check-body { flex: 1 1 auto; min-width: 0; }
.check-head { display: flex; align-items: center; gap: 8px; }
.check-title { font-weight: var(--fw-medium); }
.check-detail { font-size: 12px; color: var(--text-secondary); margin-top: 2px; word-break: break-word; }
.check-fix { display: flex; gap: 6px; font-size: 12px; margin-top: 6px; padding: 6px 8px; border-radius: var(--radius-md); background: var(--bg-subtle, transparent); border: 1px dashed var(--border); }
</style>
