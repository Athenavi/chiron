<script setup lang="ts">
/**
 * SsoLoginButtons — 三方登录按钮组
 *
 * 从 /v1/auth/sso/providers 拉取启用中的 provider，
 * 点击后整页跳转网关 SSO 登录入口（state 由后端签发）。
 * mode="bind" 时用于个人中心发起绑定（依赖登录 cookie）。
 */
import { ref, onMounted } from 'vue'
import { listPublicSsoProviders, ssoLoginURL, type SsoPublicProvider } from '../api/auth'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const props = defineProps<{ mode?: 'login' | 'bind' }>()

const providers = ref<SsoPublicProvider[]>([])
const loading = ref(false)

// provider_type 品牌样式映射（颜色徽标 + 文案）
function brandOf(p: SsoPublicProvider) {
  // 表内联在函数里：常量表在模块加载时求值一次，语言切换后 label/badge 会停在旧语言。
  const brandMap: Record<string, { color: string; label: string; badge: string }> = {
    github: { color: '#24292F', label: 'GitHub', badge: 'G' },
    wechat: { color: '#07C160', label: t('微信'), badge: t('微') },
    dingtalk: { color: '#0089FF', label: t('钉钉'), badge: t('钉') },
    feishu: { color: '#3370FF', label: t('飞书'), badge: t('飞') },
    qq: { color: '#12B7F5', label: 'QQ', badge: 'Q' },
    google: { color: '#4285F4', label: 'Google', badge: 'G' },
    custom: { color: '#722ed1', label: 'SSO', badge: 'S' },
  }
  return brandMap[p.provider_type] || brandMap.custom
}

function displayName(p: SsoPublicProvider) {
  return p.display_name || brandOf(p).label
}

onMounted(async () => {
  loading.value = true
  try {
    providers.value = await listPublicSsoProviders()
  } catch {
    // SSO 未配置或网关不可达时静默隐藏（登录页主流程不受影响）
    providers.value = []
  } finally {
    loading.value = false
  }
})

function startSso(p: SsoPublicProvider) {
  window.location.href = ssoLoginURL(p.id, props.mode === 'bind' ? 'bind' : undefined)
}
</script>

<template>
  <div
    v-if="providers.length"
    class="sso-group"
  >
    <div class="sso-divider">
      <span>{{ mode === 'bind' ? $t('绑定三方账号') : $t('三方登录') }}</span>
    </div>
    <div class="sso-buttons">
      <button
        v-for="p in providers"
        :key="p.id"
        type="button"
        class="sso-btn"
        :title="displayName(p)"
        @click="startSso(p)"
      >
        <span
          class="sso-badge"
          :style="{ background: brandOf(p).color }"
        >{{ brandOf(p).badge }}</span>
        <span class="sso-label">{{ displayName(p) }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.sso-group {
  margin-top: 4px;
}

.sso-divider {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 16px 0;
  color: var(--text-tertiary, var(--bg-overlay));
  font-size: 13px;
}

.sso-divider::before,
.sso-divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border-card, #f0f0f0);
}

.sso-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.sso-btn {
  flex: 1 1 calc(50% - 8px);
  min-width: 0;
  min-height: 40px; /* 触控目标 ≥ 40px */
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 9px 12px;
  border: 1px solid var(--border-card, #d9d9d9);
  border-radius: 8px;
  background: var(--bg-card, var(--on-solid));
  color: var(--text-primary, rgba(0, 0, 0, 0.88));
  font-size: 14px;
  cursor: pointer;
  transition: border-color var(--dur-normal), box-shadow var(--dur-normal);
}

.sso-btn:hover {
  border-color: var(--primary, #1677ff);
  box-shadow: 0 1px 4px var(--shadow-brand);
}

.sso-btn:focus-visible {
  outline: 2px solid var(--primary, #1677ff);
  outline-offset: 2px;
}

/* 窄屏（≤576px，与 .u-hide-sm 断点一致）：按钮全宽换行，易点按 */
@media (max-width: 576px) {
  .sso-btn { flex: 1 1 100%; }
}

.sso-badge {
  width: 20px;
  height: 20px;
  border-radius: 5px;
  color: var(--on-solid);
  font-size: 11px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.sso-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
