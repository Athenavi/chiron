<script setup lang="ts">
/**
 * 租户后台 · 概览。
 *
 * 一次请求聚合首页要看的几个数（成员/近 30 天用量/域名/待审发票/有效权益），
 * 后端按 claims.tenant_id 过滤，前端不传租户参数。
 */
import { onMounted, ref } from 'vue'
import { Card, Statistic, message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { getTenantOverview, type TenantOverview } from '../../api/admin'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()
const loading = ref(false)
const data = ref<TenantOverview>({
  members: 0,
  month_tokens: 0,
  month_cost_cents: 0,
  domains: 0,
  verified_domains: 0,
  pending_invoices: 0,
  active_entitlements: 0,
})

async function load() {
  loading.value = true
  try {
    data.value = await getTenantOverview()
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.failed_to_load')))
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="overview-page">
    <div class="page-header">
      <h2>{{ $t('admin.overview') }}</h2>
      <p class="page-desc">
        {{ $t('admin.overview_desc') }}
      </p>
    </div>

    <div class="stat-grid">
      <Card :loading="loading">
        <Statistic
          :title="$t('admin.overview_members')"
          :value="data.members"
        />
      </Card>
      <Card :loading="loading">
        <Statistic
          :title="$t('admin.overview_month_tokens')"
          :value="data.month_tokens"
        />
      </Card>
      <Card :loading="loading">
        <Statistic
          :title="$t('admin.overview_month_cost')"
          :value="data.month_cost_cents / 100"
          :precision="2"
          prefix="¥"
        />
      </Card>
      <Card :loading="loading">
        <Statistic
          :title="$t('admin.overview_domains')"
          :value="data.domains"
          :suffix="`(${$t('admin.overview_verified')} ${data.verified_domains})`"
        />
      </Card>
      <Card :loading="loading">
        <Statistic
          :title="$t('admin.overview_pending_invoices')"
          :value="data.pending_invoices"
        />
      </Card>
      <Card :loading="loading">
        <Statistic
          :title="$t('admin.overview_active_entitlements')"
          :value="data.active_entitlements"
        />
      </Card>
    </div>
  </div>
</template>

<style scoped>
.overview-page { padding: 4px; }
.page-header { margin-bottom: 16px; }
.page-header h2 { margin: 0; font-size: 18px; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-tertiary); }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
</style>
