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
    const gw = data.gateway || {} as any
    const py = data.python_engine || {} as any

    metrics.value = {
      connections: gw.connections || 0,
      avgLatencyMs: py.avg_inference_ms || 0,
      dbLatencyMs: gw.db_latency_ms || 0,
    }

    gatewayStatus.value = {
      instances: gw.instances || 0,
      cpuUsage: gw.cpu_percent || 0,
      memoryUsage: formatMemory(gw.memory_mb || 0),
      goroutines: gw.goroutines || 0,
      connections: gw.connections || 0,
      redisLatency: gw.redis_latency_ms || 0,
      dbLatency: gw.db_latency_ms || 0,
      uptime: gw.uptime_seconds ? formatUptime(gw.uptime_seconds) : '--',
      version: gw.version || '--',
    }

    pythonStatus.value = {
      pods: py.pods || 0,
      cpuUsage: py.cpu_percent || 0,
      memoryUsage: formatMemory(py.memory_mb || 0),
      activeTasks: py.active_tasks || 0,
      avgInferenceTime: py.avg_inference_ms || 0,
      redisLatency: py.redis_latency_ms || 0,
      uptime: py.uptime_seconds ? formatUptime(py.uptime_seconds) : '--',
      version: py.version || '--',
    }
  } catch {
    message.error(t('获取性能数据失败'))
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
            :title="$t('并发连接数')"
            :value="metrics.connections"
          />
        </Card>
        <Card>
          <Statistic
            :title="$t('推理延迟(均值)')"
            :value="metrics.avgLatencyMs"
            suffix="ms"
          />
        </Card>
        <Card>
          <Statistic
            :title="$t('网关 DB 延迟')"
            :value="metrics.dbLatencyMs"
            suffix="ms"
          />
        </Card>
      </div>

      <Card
        :title="$t('Go 网关状态')"
        style="margin-top: 16px"
      >
        <div class="status-grid">
          <Descriptions
            bordered
            :column="1"
          >
            <DescriptionsItem :label="$t('实例数')">
              {{ gatewayStatus.instances }}
            </DescriptionsItem>
            <DescriptionsItem :label="$t('CPU 使用率')">
              {{ gatewayStatus.cpuUsage }}%
            </DescriptionsItem>
            <DescriptionsItem :label="$t('内存使用')">
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
            <DescriptionsItem :label="$t('连接数')">
              {{ gatewayStatus.connections }}
            </DescriptionsItem>
            <DescriptionsItem :label="$t('Redis 延迟')">
              {{ gatewayStatus.redisLatency }}ms
            </DescriptionsItem>
            <DescriptionsItem :label="$t('DB 延迟')">
              {{ gatewayStatus.dbLatency }}ms
            </DescriptionsItem>
            <DescriptionsItem :label="$t('运行时间')">
              {{ gatewayStatus.uptime }}
            </DescriptionsItem>
          </Descriptions>
          <Descriptions
            bordered
            :column="1"
          >
            <DescriptionsItem :label="$t('版本')">
              {{ gatewayStatus.version }}
            </DescriptionsItem>
          </Descriptions>
        </div>
      </Card>

      <Card
        :title="$t('Python 引擎状态')"
        style="margin-top: 16px"
      >
        <div class="status-grid">
          <Descriptions
            bordered
            :column="1"
          >
            <DescriptionsItem :label="$t('Pod 数量')">
              {{ pythonStatus.pods }}
            </DescriptionsItem>
            <DescriptionsItem :label="$t('CPU 使用率')">
              {{ pythonStatus.cpuUsage }}%
            </DescriptionsItem>
            <DescriptionsItem :label="$t('内存使用')">
              {{ pythonStatus.memoryUsage }}
            </DescriptionsItem>
            <DescriptionsItem :label="$t('活跃任务')">
              {{ pythonStatus.activeTasks }}
            </DescriptionsItem>
          </Descriptions>
          <Descriptions
            bordered
            :column="1"
          >
            <DescriptionsItem :label="$t('平均推理时间')">
              {{ pythonStatus.avgInferenceTime }}ms
            </DescriptionsItem>
            <DescriptionsItem :label="$t('Redis 延迟')">
              {{ pythonStatus.redisLatency }}ms
            </DescriptionsItem>
            <DescriptionsItem :label="$t('运行时间')">
              {{ pythonStatus.uptime }}
            </DescriptionsItem>
            <DescriptionsItem :label="$t('版本')">
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
