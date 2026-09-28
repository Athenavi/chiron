<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Card, Statistic, Descriptions, DescriptionsItem, Spin, message } from 'ant-design-vue'
import { getPerformance } from '@/api/admin'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const loading = ref(false)

const metrics = ref({
  connections: 0,
  avgLatencyMs: 0,
  dbLatencyMs: 0,
})

const gatewayStatus = ref({
  instances: 0,
  cpuUsage: 0,
  memoryUsage: '0 MB',
  goroutines: 0,
  connections: 0,
  redisLatency: 0,
  dbLatency: 0,
  uptime: '--',
  version: '--',
})

const pythonStatus = ref({
  pods: 0,
  cpuUsage: 0,
  memoryUsage: '0 MB',
  activeTasks: 0,
  avgInferenceTime: 0,
  redisLatency: 0,
  uptime: '--',
  version: '--',
})

function formatMemory(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`
  return `${mb} MB`
}

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return `${days}d ${hours}h ${minutes}m`
}

async function fetchData() {
  loading.value = true
  try {
    const data = await getPerformance()
    const gw = (data.gateway || {}) as Record<string, unknown>
    const py = (data.python_engine || {}) as Record<string, unknown>
    // 网关/引擎的指标字段来自 JSON（形状由后端保证）：数值化一次，避免把字符串塞进 number 槽位
    const num = (v: unknown): number => Number(v) || 0
    const strOf = (v: unknown): string => (typeof v === 'string' && v ? v : '--')

    metrics.value = {
      connections: num(gw.connections),
      avgLatencyMs: num(py.avg_inference_ms),
      dbLatencyMs: num(gw.db_latency_ms),
    }

    gatewayStatus.value = {
      instances: num(gw.instances),
      cpuUsage: num(gw.cpu_percent),
      memoryUsage: formatMemory(num(gw.memory_mb)),
      goroutines: num(gw.goroutines),
      connections: num(gw.connections),
      redisLatency: num(gw.redis_latency_ms),
      dbLatency: num(gw.db_latency_ms),
      uptime: num(gw.uptime_seconds) ? formatUptime(num(gw.uptime_seconds)) : '--',
      version: strOf(gw.version),
    }

    pythonStatus.value = {
      pods: num(py.pods),
      cpuUsage: num(py.cpu_percent),
      memoryUsage: formatMemory(num(py.memory_mb)),
      activeTasks: num(py.active_tasks),
      avgInferenceTime: num(py.avg_inference_ms),
      redisLatency: num(py.redis_latency_ms),
      uptime: num(py.uptime_seconds) ? formatUptime(num(py.uptime_seconds)) : '--',
      version: strOf(py.version),
    }
  } catch {
    message.error(t('errors.failed_to_fetch_performance_data'))
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchData()
})
</script>

<template>
  <div class="performance-monitor">
    <Spin :spinning="loading">
      <div class="metric-grid">
        <Card>
          <Statistic
            :title="$t('common.concurrent_connection_count')"
            :value="metrics.connections"
          />
        </Card>
        <Card>
          <Statistic
            :title="$t('chat.inference_latency_avg')"
            :value="metrics.avgLatencyMs"
            suffix="ms"
          />
        </Card>
        <Card>
          <Statistic
            :title="$t('common.gateway_db_latency')"
            :value="metrics.dbLatencyMs"
            suffix="ms"
          />
        </Card>
      </div>

      <Card
        :title="$t('common.go_gateway_status')"
        style="margin-top: 16px"
      >
        <div class="status-grid">
          <Descriptions
            bordered
            :column="1"
          >
            <DescriptionsItem :label="$t('common.instances')">
              {{ gatewayStatus.instances }}
            </DescriptionsItem>
            <DescriptionsItem :label="$t('common.cpu_usage')">
              {{ gatewayStatus.cpuUsage }}%
            </DescriptionsItem>
            <DescriptionsItem :label="$t('memory.memory_usage')">
              {{ gatewayStatus.memoryUsage }}
            </DescriptionsItem>
            <DescriptionsItem label="Goroutines">
              {{ gatewayStatus.goroutines }}
            </DescriptionsItem>
          </Descriptions>
          <Descriptions
            bordered
            :column="1"
          >
            <DescriptionsItem :label="$t('common.connections')">
              {{ gatewayStatus.connections }}
            </DescriptionsItem>
            <DescriptionsItem :label="$t('admin.redis_latency')">
              {{ gatewayStatus.redisLatency }}ms
            </DescriptionsItem>
            <DescriptionsItem :label="$t('common.db_latency')">
              {{ gatewayStatus.dbLatency }}ms
            </DescriptionsItem>
            <DescriptionsItem :label="$t('common.run_time')">
              {{ gatewayStatus.uptime }}
            </DescriptionsItem>
          </Descriptions>
          <Descriptions
            bordered
            :column="1"
          >
            <DescriptionsItem :label="$t('common.version')">
              {{ gatewayStatus.version }}
            </DescriptionsItem>
          </Descriptions>
        </div>
      </Card>

      <Card
        :title="$t('common.python_engine_status')"
        style="margin-top: 16px"
      >
        <div class="status-grid">
          <Descriptions
            bordered
            :column="1"
          >
            <DescriptionsItem :label="$t('common.pod_count')">
              {{ pythonStatus.pods }}
            </DescriptionsItem>
            <DescriptionsItem :label="$t('common.cpu_usage')">
              {{ pythonStatus.cpuUsage }}%
            </DescriptionsItem>
            <DescriptionsItem :label="$t('memory.memory_usage')">
              {{ pythonStatus.memoryUsage }}
            </DescriptionsItem>
            <DescriptionsItem :label="$t('workflow.active_tasks')">
              {{ pythonStatus.activeTasks }}
            </DescriptionsItem>
          </Descriptions>
          <Descriptions
            bordered
            :column="1"
          >
            <DescriptionsItem :label="$t('chat.avg_inference_time')">
              {{ pythonStatus.avgInferenceTime }}ms
            </DescriptionsItem>
            <DescriptionsItem :label="$t('admin.redis_latency')">
              {{ pythonStatus.redisLatency }}ms
            </DescriptionsItem>
            <DescriptionsItem :label="$t('common.run_time')">
              {{ pythonStatus.uptime }}
            </DescriptionsItem>
            <DescriptionsItem :label="$t('common.version')">
              {{ pythonStatus.version }}
            </DescriptionsItem>
          </Descriptions>
        </div>
      </Card>
    </Spin>
  </div>
</template>

<style scoped>
.performance-monitor { padding: 0; }

/* 指标卡网格:auto-fill(minmax 150px),窄屏自动降列 */
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 16px;
}

/* 状态卡片网格 */
.status-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
  margin-top: 16px;
}
</style>
