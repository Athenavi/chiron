<script setup lang="ts">
/**
 * 知识库设置 — provider 二选一（自托管 / PageIndex Cloud）。
 *
 * 这是本轮重构新增的页面：旧实现没有"用哪套检索引擎"这个概念（wiki 与 rag 是知识库类型），
 * 现在每个用户必须选一个 —— 自托管用部署方配置的模型与共享索引目录，Cloud 用用户自己的
 * PageIndex API key。切换前必须先清空使用另一种 provider 的知识库（后端会返回 409）。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Alert, Button, Input, Radio, Spin, Tag, message } from 'ant-design-vue'
import { ArrowLeftOutlined, CloudServerOutlined, CloudOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import {
  getKnowledgeSettings,
  putKnowledgeSettings,
  verifyKnowledgeSettings,
  type KnowledgeSettings,
  type ProviderName,
} from '../api/knowledge'
import { serverErrorDetail } from '../utils/apiError'

const { t } = useI18n()
const router = useRouter()

const loading = ref(true)
const saving = ref(false)
const verifying = ref(false)
const settings = ref<KnowledgeSettings | null>(null)
const provider = ref<ProviderName>('selfhosted')
const cloudKey = ref('')
const verifyResult = ref<{ ok: boolean; detail: string } | null>(null)

const selfhosted = computed(() => settings.value?.providers.find(p => p.name === 'selfhosted'))
const cloud = computed(() => settings.value?.providers.find(p => p.name === 'cloud'))
const cloudConfigured = computed(() => settings.value?.cloud_key_configured ?? false)

async function load() {
  loading.value = true
  try {
    const data = await getKnowledgeSettings()
    settings.value = data
    // 未选择时默认停在自托管上，但不替用户保存 —— "默认值"与"已选定"必须可区分
    provider.value = (data.provider || 'selfhosted') as ProviderName
  } catch (e) {
    message.error(serverErrorDetail(e, t('errors.failed_to_load')))
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  verifyResult.value = null
  try {
    const body: { provider: ProviderName; cloud_api_key?: string } = { provider: provider.value }
    if (provider.value === 'cloud' && cloudKey.value.trim()) body.cloud_api_key = cloudKey.value.trim()
    const saved = await putKnowledgeSettings(body)
    cloudKey.value = ''
    message.success(t('knowledge.settings_saved'))
    settings.value = { ...(settings.value as KnowledgeSettings), provider: saved.provider as ProviderName, status: saved.status, cloud_key_configured: saved.cloud_key_configured }
  } catch (e) {
    message.error(serverErrorDetail(e, t('errors.save_failed')))
  } finally {
    saving.value = false
  }
}

async function verify() {
  verifying.value = true
  verifyResult.value = null
  try {
    const result = await verifyKnowledgeSettings()
    verifyResult.value = { ok: result.ok, detail: result.detail }
  } catch (e) {
    message.error(serverErrorDetail(e, t('errors.operation_failed')))
  } finally {
    verifying.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="kb-settings-page">
    <div class="page-head">
      <Button type="text" class="back-btn" @click="router.push('/knowledge')">
        <ArrowLeftOutlined />
      </Button>
      <div class="page-head-text">
        <h1 class="page-title">{{ t('knowledge.knowledge_settings') }}</h1>
        <p class="page-sub">{{ t('knowledge.knowledge_settings_hint') }}</p>
      </div>
    </div>

    <PageSkeleton v-if="loading" variant="detail" />

    <template v-else>
      <Alert
        v-if="settings?.error_message"
        type="error"
        show-icon
        :message="settings.error_message"
        class="page-alert"
      />

      <div class="provider-grid">
        <label class="provider-card" :class="{ 'is-active': provider === 'selfhosted' }" @click="provider = 'selfhosted'">
          <Radio :checked="provider === 'selfhosted'" />
          <CloudServerOutlined class="provider-icon" />
          <div class="provider-text">
            <div class="provider-title">
              {{ t('knowledge.provider_selfhosted') }}
              <Tag v-if="selfhosted?.available" color="green">{{ t('knowledge.ready') }}</Tag>
              <Tag v-else color="default">{{ t('knowledge.not_configured') }}</Tag>
            </div>
            <p class="provider-desc">{{ t('knowledge.provider_selfhosted_desc') }}</p>
            <p v-if="selfhosted?.detail" class="provider-detail">{{ selfhosted.detail }}</p>
          </div>
        </label>

        <label class="provider-card" :class="{ 'is-active': provider === 'cloud' }" @click="provider = 'cloud'">
          <Radio :checked="provider === 'cloud'" />
          <CloudOutlined class="provider-icon" />
          <div class="provider-text">
            <div class="provider-title">
              {{ t('knowledge.provider_cloud') }}
              <Tag v-if="cloudConfigured" color="green">{{ t('knowledge.configured') }}</Tag>
              <Tag v-else color="default">{{ t('knowledge.not_configured') }}</Tag>
            </div>
            <p class="provider-desc">{{ t('knowledge.provider_cloud_desc') }}</p>
            <p class="provider-detail">{{ t('knowledge.provider_cloud_note') }}</p>
          </div>
        </label>
      </div>

      <div v-if="provider === 'cloud'" class="key-block">
        <label class="key-label">{{ t('knowledge.cloud_api_key') }}</label>
        <Input.Password
          v-model:value="cloudKey"
          :placeholder="cloudConfigured ? t('knowledge.cloud_api_key_keep_hint') : 'px-...'"
          autocomplete="off"
        />
        <p class="key-hint">{{ t('knowledge.cloud_api_key_hint') }}</p>
      </div>

      <Alert
        v-if="verifyResult"
        class="page-alert"
        :type="verifyResult.ok ? 'success' : 'error'"
        show-icon
        :message="verifyResult.ok ? t('knowledge.connection_ok') : verifyResult.detail"
      />

      <div class="actions">
        <Button type="primary" :loading="saving" @click="save">
          {{ t('common.save') }}
        </Button>
        <Button :loading="verifying" :disabled="!settings?.provider" @click="verify">
          {{ t('knowledge.test_connection') }}
        </Button>
        <Spin v-if="loading" size="small" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.kb-settings-page {
  padding: 24px;
  max-width: 860px;
  margin: 0 auto;
}
.page-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 20px;
}
.page-title {
  margin: 0;
  font-size: 20px;
  font-weight: var(--fw-semibold);
}
.page-sub {
  margin: 4px 0 0;
  color: var(--ant-color-text-secondary, #8c8c8c);
  font-size: 13px;
}
.page-alert {
  margin-bottom: 16px;
}
.provider-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}
.provider-card {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--ant-color-border, #d9d9d9);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color var(--dur-normal), box-shadow var(--dur-normal);
}
.provider-card.is-active {
  border-color: var(--ant-color-primary, #1677ff);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary) 8%, transparent);
}
.provider-icon {
  font-size: 20px;
  margin-top: 2px;
}
.provider-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: var(--fw-semibold);
}
.provider-desc {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--ant-color-text-secondary, #8c8c8c);
}
.provider-detail {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--ant-color-warning, #d48806);
}
.key-block {
  margin-bottom: 20px;
}
.key-label {
  display: block;
  margin-bottom: 6px;
  font-weight: var(--fw-medium);
}
.key-hint {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--ant-color-text-secondary, #8c8c8c);
}
.actions {
  display: flex;
  gap: 12px;
  align-items: center;
}
</style>
