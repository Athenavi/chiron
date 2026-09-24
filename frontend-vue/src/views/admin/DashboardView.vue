<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, markRaw } from 'vue'
import { useRouter } from 'vue-router'
import { Card, Row, Col, Statistic, Tag, Table } from 'ant-design-vue'
import {
  KeyOutlined, ThunderboltOutlined, SettingOutlined,
  ArrowRightOutlined, ArrowUpOutlined, ArrowDownOutlined, ClockCircleOutlined,
  ApartmentOutlined, ThunderboltFilled, DatabaseFilled,
} from '@ant-design/icons-vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, PieChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { getMetrics, listApiKeys } from '@/api/admin'
import { useThemeStore } from '@/stores/theme'
import PageSkeleton from '@/components/common/PageSkeleton.vue'
import EmptyState from '@/components/common/EmptyState.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
use([CanvasRenderer, LineChart, PieChart, GridComponent, TooltipComponent, LegendComponent])

const router = useRouter()
const themeStore = useThemeStore()
const loading = ref(true)
const error = ref('')

// 快捷导航：三个监控页已合并为「运行时监控」，定时任务已拆为独立页面
const navItems = [
  { label: t('运行时监控'), path: '/admin/monitor', icon: markRaw(ThunderboltOutlined), desc: t('性能 / 队列 / 缓存') },
  { label: 'API Key', path: '/admin/api-keys', icon: markRaw(KeyOutlined), desc: t('密钥与限流') },
  { label: t('定时任务'), path: '/admin/cron', icon: markRaw(ClockCircleOutlined), desc: t('自动化调度') },
  { label: t('系统设置'), path: '/admin/settings', icon: markRaw(SettingOutlined), desc: t('全局配置') },
]

const stats = ref({
  connections: 0,
  connectionsTrend: 0,
  queueBacklog: 0,
  cacheHitRate: 0,
  latencyP99: 0,
})

// 并发连接趋势：由 /v1/admin/metrics 的 concurrent_connections 按采样间隔累积。
// 此前这里是 7 个硬编码的零点且从不更新，趋势图恒为一条零线。
const connectionHistory = ref<{ time: string; value: number }[]>([])
const HISTORY_MAX = 20

function pushConnectionSample(value: number) {
  const d = new Date()
  const time = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  connectionHistory.value.push({ time, value })
  if (connectionHistory.value.length > HISTORY_MAX) connectionHistory.value.shift()
}

// ECharts 主题色：跟随设计系统（亮/暗）
const chartColors = computed(() => {
  const isDark = themeStore.isDark
  return {
    primary: isDark ? '#5686fe' : '#4176e6',
    text: isDark ? '#cfd3d6' : '#61666b',
    textSecondary: isDark ? '#adb2b8' : '#81858c',
    splitLine: isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)',
    success: '#22c55e',
    warning: '#f59e0b',
    error: '#ef4444',
    bg: isDark ? '#2c2c2e' : '#f1f3f5',
  }
})

const connectionChartOption = computed(() => ({
  tooltip: {
    trigger: 'axis',
    backgroundColor: chartColors.value.bg,
    borderColor: chartColors.value.splitLine,
    textStyle: { color: chartColors.value.text, fontSize: 12 },
  },
  grid: { left: 40, right: 20, top: 20, bottom: 28 },
  xAxis: {
    type: 'category',
    data: connectionHistory.value.map(h => h.time),
    axisLabel: { color: chartColors.value.textSecondary, fontSize: 11 },
    axisLine: { lineStyle: { color: chartColors.value.splitLine } },
    axisTick: { show: false },
  },
  yAxis: {
    type: 'value',
    name: '连接数',
    nameTextStyle: { color: chartColors.value.textSecondary, fontSize: 11 },
    axisLabel: { color: chartColors.value.textSecondary, fontSize: 11 },
    splitLine: { lineStyle: { color: chartColors.value.splitLine } },
    axisLine: { show: false },
    axisTick: { show: false },
  },
  series: [{
    name: '并发连接',
    type: 'line',
    data: connectionHistory.value.map(h => h.value),
    smooth: true,
    symbol: 'none',
    lineStyle: { color: chartColors.value.primary, width: 2 },
    areaStyle: {
      color: {
        type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [
          { offset: 0, color: chartColors.value.primary + '4D' },
          { offset: 1, color: chartColors.value.primary + '0D' },
        ],
      },
    },
  }],
}))

const apiKeyStatus = ref({ active: 0, rate_limited: 0, circuit_open: 0 })

const apiKeyChartOption = computed(() => ({
  tooltip: {
    trigger: 'item',
    backgroundColor: chartColors.value.bg,
    borderColor: chartColors.value.splitLine,
    textStyle: { color: chartColors.value.text, fontSize: 12 },
  },
  legend: {
    bottom: 0,
    icon: 'circle',
    itemWidth: 8, itemHeight: 8,
    textStyle: { color: chartColors.value.textSecondary, fontSize: 12 },
  },
  series: [{
    name: 'API Key 状态',
    type: 'pie',
    radius: ['52%', '72%'],
    center: ['50%', '44%'],
    avoidLabelOverlap: true,
    itemStyle: { borderColor: chartColors.value.bg, borderWidth: 2 },
    label: { show: false },
    data: [
      { value: apiKeyStatus.value.active || 1, name: '正常', itemStyle: { color: chartColors.value.success } },
      { value: apiKeyStatus.value.rate_limited || 0, name: '限流中', itemStyle: { color: chartColors.value.warning } },
      { value: apiKeyStatus.value.circuit_open || 0, name: '熔断', itemStyle: { color: chartColors.value.error } },
    ],
  }],
}))

const alertColumns = [
  { title: t('时间'), dataIndex: 'time', width: 180 },
  { title: t('级别'), dataIndex: 'level', width: 100 },
  { title: t('消息'), dataIndex: 'message' },
]

const alerts = ref<{ time: string; level: string; message: string }[]>([])

async function fetchDashboardData() {
  loading.value = true
  error.value = ''
  try {
    const [metricsRes, keysRes] = await Promise.allSettled([
      getMetrics(),
      listApiKeys(),
    ])

    if (metricsRes.status === 'fulfilled') {
      const m = metricsRes.value
      stats.value.connections = m.concurrent_connections || 0
      stats.value.queueBacklog = m.queue_backlog || 0
      stats.value.cacheHitRate = m.cache_hit_rate || 0
      stats.value.latencyP99 = m.api_latency_p99 || 0
      pushConnectionSample(stats.value.connections)
    }

    if (keysRes.status === 'fulfilled') {
      const keys = keysRes.value
      apiKeyStatus.value = {
        active: keys.filter((k: any) => k.status === 'active').length,
        rate_limited: keys.filter((k: any) => k.status === 'rate_limited').length,
        circuit_open: keys.filter((k: any) => k.status === 'circuit_open').length,
      }
    }
  } catch {
    error.value = '数据加载失败'
  } finally {
    loading.value = false
  }
}

// 趋势图需要多点采样：首次加载 + 每 30s 轮询，累积最近 HISTORY_MAX 个点。
const POLL_INTERVAL_MS = 30_000
let pollTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  void fetchDashboardData()
  pollTimer = setInterval(() => {
    void fetchDashboardData()
  }, POLL_INTERVAL_MS)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="dashboard">
    <!-- 骨架屏（加载态） -->
    <PageSkeleton
      v-if="loading"
      variant="cards"
      :columns="2"
      header
    />

    <!-- 错误态 -->
    <EmptyState
      v-else-if="error"
      size="page"
      :description="error"
      :hint="$t('请检查后端服务是否正常，或稍后重试')"
    >
      <a-button
        type="primary"
        size="large"
        @click="fetchDashboardData"
      >
        {{ $t('重试') }}
      </a-button>
    </EmptyState>

    <template v-else>
      <!-- 快捷导航卡片 -->
      <div class="nav-grid">
        <div
          v-for="nav in navItems"
          :key="nav.path"
          class="nav-card"
          role="link"
          tabindex="0"
          @click="router.push(nav.path)"
          @keydown.enter="router.push(nav.path)"
        >
          <div class="nav-card-icon">
            <component :is="nav.icon" />
          </div>
          <div class="nav-card-body">
            <div class="nav-card-label">
              {{ nav.label }}
            </div>
            <div class="nav-card-desc">
              {{ nav.desc }}
            </div>
          </div>
          <ArrowRightOutlined class="nav-card-arrow" />
        </div>
      </div>

      <!-- 统计卡片 -->
      <div class="stats-grid">
        <Card
          class="stat-card"
          :bordered="false"
        >
          <Statistic
            :title="$t('并发连接')"
            :value="stats.connections"
            :value-style="{ color: 'var(--text-primary)', fontSize: '28px', fontFamily: 'var(--font-sans)', fontVariantNumeric: 'tabular-nums' }"
          >
            <template #prefix>
              <ApartmentOutlined class="stat-icon" />
            </template>
            <template #suffix>
              <Tag
                v-if="stats.connectionsTrend !== 0"
                :color="stats.connectionsTrend > 0 ? 'success' : 'error'"
                class="stat-trend"
              >
                <component :is="stats.connectionsTrend > 0 ? ArrowUpOutlined : ArrowDownOutlined" />
                {{ Math.abs(stats.connectionsTrend) }}%
              </Tag>
            </template>
          </Statistic>
        </Card>
        <Card
          class="stat-card"
          :bordered="false"
        >
          <Statistic
            :title="$t('队列积压')"
            :value="stats.queueBacklog"
            :value-style="{ color: 'var(--text-primary)', fontSize: '28px', fontVariantNumeric: 'tabular-nums' }"
          >
            <template #prefix>
              <OrderedListOutlined class="stat-icon" />
            </template>
            <template #suffix>
              <Tag
                :color="stats.queueBacklog > 1000 ? 'error' : 'success'"
                class="stat-trend"
              >
                {{ stats.queueBacklog > 1000 ? '告警' : '正常' }}
              </Tag>
            </template>
          </Statistic>
        </Card>
        <Card
          class="stat-card"
          :bordered="false"
        >
          <Statistic
            :title="$t('缓存命中率')"
            :value="stats.cacheHitRate"
            suffix="%"
            :value-style="{ color: 'var(--text-primary)', fontSize: '28px', fontVariantNumeric: 'tabular-nums' }"
          >
            <template #prefix>
              <DatabaseFilled class="stat-icon" />
            </template>
          </Statistic>
        </Card>
        <Card
          class="stat-card"
          :bordered="false"
        >
          <Statistic
            :title="$t('API 延迟 P99')"
            :value="stats.latencyP99"
            suffix="ms"
            :value-style="{ color: 'var(--text-primary)', fontSize: '28px', fontVariantNumeric: 'tabular-nums' }"
          >
            <template #prefix>
              <ThunderboltFilled class="stat-icon" />
            </template>
          </Statistic>
        </Card>
      </div>

      <!-- 图表区域 -->
      <Row
        :gutter="16"
        class="charts-row"
      >
        <Col
          :xs="24"
          :lg="12"
        >
          <Card
            class="chart-card"
            :bordered="false"
          >
            <template #title>
              <span class="chart-title">
                <ApartmentOutlined class="chart-title-icon" /> {{ $t('并发连接趋势') }}
              </span>
            </template>
            <VChart
              :option="connectionChartOption"
              class="chart-canvas"
              autoresize
            />
          </Card>
        </Col>
        <Col
          :xs="24"
          :lg="12"
        >
          <Card
            class="chart-card"
            :bordered="false"
          >
            <template #title>
              <span class="chart-title">
                <KeyOutlined class="chart-title-icon" /> {{ $t('API Key 状态') }}
              </span>
            </template>
            <VChart
              :option="apiKeyChartOption"
              class="chart-canvas"
              autoresize
            />
          </Card>
        </Col>
      </Row>

      <!-- 告警列表 -->
      <Card
        class="alert-card"
        :bordered="false"
      >
        <template #title>
          <span class="chart-title">
            <ClockCircleOutlined class="chart-title-icon" /> {{ $t('最近告警') }}
          </span>
        </template>
        <EmptyState
          v-if="alerts.length === 0"
          size="list"
          :description="$t('暂无告警')"
          :hint="$t('系统运行正常')"
        />
        <Table
          v-else
          :columns="alertColumns"
          :data-source="alerts"
          :pagination="false"
          size="small"
        />
      </Card>
    </template>


  </div>
</template>

<style scoped>
.dashboard { display: flex; flex-direction: column; gap: 16px; }

/* ── 快捷导航卡片网格 ── */
.nav-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}
.nav-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid var(--border-card);
  border-radius: 10px;
  background: var(--bg-card);
  cursor: pointer;
  transition: border-color var(--dur-fast) ease, box-shadow var(--dur-fast) ease, transform var(--dur-fast) ease;
}
.nav-card:hover {
  border-color: var(--primary);
  box-shadow: var(--shadow-md);
  transform: translateY(-1px);
}
.nav-card:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.nav-card:active { transform: translateY(0); }
.nav-card-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: var(--primary-bg);
  color: var(--primary);
  font-size: 16px;
  flex-shrink: 0;
}
.nav-card-body { flex: 1; min-width: 0; }
.nav-card-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.nav-card-desc {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-top: 1px;
}
.nav-card-arrow {
  font-size: 12px;
  color: var(--text-muted);
  transition: transform var(--dur-fast) ease, color var(--dur-fast) ease;
}
.nav-card:hover .nav-card-arrow { color: var(--primary); transform: translateX(2px); }

/* ── 统计卡片 ── */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
}
.stat-card {
  border-radius: 10px !important;
  background: var(--bg-card) !important;
  box-shadow: var(--shadow-md);
  min-width: 0;
}
.stat-card :deep(.ant-card-body) { padding: 18px 20px; }
.stat-card :deep(.ant-statistic-title) {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-bottom: 6px;
}
.stat-icon { font-size: 18px; color: var(--primary); margin-right: 4px; }
.stat-trend { font-size: 11px; padding: 0 6px; border-radius: 4px; margin-left: 4px; }

/* ── 图表卡片 ── */
.charts-row { margin-bottom: 0 !important; }
.chart-card {
  border-radius: 10px !important;
  background: var(--bg-card) !important;
  box-shadow: var(--shadow-md);
}
.chart-card :deep(.ant-card-head) {
  min-height: 44px;
  border-bottom: 1px solid var(--border-card);
  padding: 0 16px;
}
.chart-card :deep(.ant-card-body) { padding: 12px 16px 16px; }
.chart-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.chart-title-icon { color: var(--primary); font-size: 15px; }
.chart-canvas { height: 280px; }

/* ── 告警卡片 ── */
.alert-card {
  border-radius: 10px !important;
  background: var(--bg-card) !important;
  box-shadow: var(--shadow-md);
}
.alert-card :deep(.ant-card-body) { padding: 0 16px 16px; }
.alert-card :deep(.ant-table) {
  background: transparent;
}
.alert-card :deep(.ant-table-thead > tr > th) {
  background: var(--bg-secondary);
  font-size: 12px;
  font-weight: 600;
  color: var(--text-tertiary);
  border-bottom: 1px solid var(--border-card);
}
.alert-card :deep(.ant-table-tbody > tr > td) {
  border-bottom: 1px solid var(--border-card);
  font-size: 13px;
}
.alert-card :deep(.ant-table-tbody > tr:hover > td) {
  background: var(--bg-hover) !important;
}

/* 移动端：导航卡片两列 → 单列；统计/图表高度压缩 */
@media (max-width: 768px) {
  .nav-grid { grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); }
  .chart-canvas { height: 220px; }
}
@media (max-width: 576px) {
  .nav-grid { grid-template-columns: 1fr; }
  .chart-canvas { height: 200px; }
  .stat-card :deep(.ant-card-body) { padding: 14px 16px; }
  .stat-card :deep(.ant-statistic-content-value) { font-size: 22px !important; }
}


</style>
