<script setup lang="ts">
import AdminTabbedView from '@/components/admin/AdminTabbedView.vue'
import PerformanceView from './PerformanceView.vue'
import QueueView from './QueueView.vue'
import CacheView from './CacheView.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
/**
 * 运行时监控 = 原「性能监控 / 队列监控 / 缓存监控」三页合并。
 * 三者是同一类信息（运行时状态）、体量都很小（模板各 ~100-140 行）却各占一个菜单项；
 * 合并且各面板原样复用后，职责边界清晰：仪表盘 = 概览，本页 = 排障。
 * 懒加载与 ?tab= 同步由 AdminTabbedView 统一处理。
 */
const tabs = [
  { key: 'performance', label: t('admin.performance'), comp: PerformanceView },
  { key: 'queue', label: t('admin.queue'), comp: QueueView },
  { key: 'cache', label: t('admin.cache'), comp: CacheView },
]
</script>

<template>
  <AdminTabbedView
    route-path="/admin/monitor"
    default-tab="performance"
    :tabs="tabs"
  />
</template>
