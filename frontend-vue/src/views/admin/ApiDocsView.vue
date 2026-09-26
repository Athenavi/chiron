<script setup lang="ts">
import { computed } from 'vue'

const specUrl = computed(() => {
  const origin = window.location.origin
  const apiOrigin = origin.replace(/:5173(?=\/|$)/, ':8080')
  return `${apiOrigin}/docs/openapi.yaml`
})

// redoc CDN 展示
const redocUrl = computed(() =>
  `https://redocly.github.io/redoc/?spec-url=${encodeURIComponent(specUrl.value)}`
)
</script>

<template>
  <div class="api-docs-page">
    <div class="api-docs-header">
      <h2>{{ $t('knowledge.api_documentation') }}</h2>
      <p class="api-docs-desc">
        {{ $t('knowledge.complete_chiron_api_documentation_openapi_3_0_covering_auth_chat_enterprise_features_sso_system_monitoring_etc') }}
      </p>
    </div>
    <div class="redoc-wrapper">
      <iframe
        :src="redocUrl"
        frameborder="0"
        class="redoc-frame"
        :title="$t('knowledge.chiron_api_documentation')"
      />
    </div>
  </div>
</template>

<style scoped>
.api-docs-page {
  padding: 0;
  height: 100%;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.api-docs-header {
  margin-bottom: 16px;
  flex-shrink: 0;
}

.api-docs-header h2 {
  margin: 0 0 4px;
}

.api-docs-desc {
  color: var(--text-secondary);
  margin: 0;
  font-size: 14px;
}

.redoc-wrapper {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
}

.redoc-frame {
  width: 100%;
  height: 100%;
  border: none;
}

/* 移动端 */
@media (max-width: 640px) {
  .api-docs-header h2 { font-size: 18px; }
  .api-docs-desc { font-size: 13px; }
}
</style>
