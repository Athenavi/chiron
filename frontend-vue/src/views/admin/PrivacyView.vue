<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { getPrivacy, putPrivacy } from '../../api/policy'
import type { TenantPrivacy } from '../../api/policy'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const loading = ref(false)
const saving = ref(false)
const form = ref({
  privacy_mode: false,
  data_retention_days: 0,
  training_allowed: true,
  redaction_rules: '{}',
})

async function fetchPrivacy() {
  loading.value = true
  try {
    const p: TenantPrivacy = await getPrivacy()
    form.value = {
      privacy_mode: p.privacy_mode,
      data_retention_days: p.data_retention_days,
      training_allowed: p.training_allowed,
      redaction_rules: typeof p.redaction_rules === 'string'
        ? p.redaction_rules
        : JSON.stringify(p.redaction_rules ?? {}, null, 2),
    }
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('加载失败'))
  } finally {
    loading.value = false
  }
}

async function save() {
  let rules: unknown
  try {
    rules = JSON.parse(form.value.redaction_rules || '{}')
  } catch {
    message.error(t('脱敏规则必须是合法 JSON'))
    return
  }
  saving.value = true
  try {
    await putPrivacy({
      privacy_mode: form.value.privacy_mode,
      data_retention_days: form.value.data_retention_days,
      training_allowed: form.value.training_allowed,
      redaction_rules: rules,
    })
    message.success(t('已保存'))
    fetchPrivacy()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('保存失败'))
  } finally {
    saving.value = false
  }
}

onMounted(fetchPrivacy)
</script>

<template>
  <div class="privacy-view">
    <div class="page-header">
      <h2 class="page-title">
        {{ $t('隐私模式管控') }}
      </h2>
    </div>

    <a-spin :spinning="loading">
      <a-card
        :title="$t('租户隐私策略')"
        style="max-width: 720px"
      >
        <a-form layout="vertical">
          <a-form-item :label="$t('隐私模式')">
            <a-switch v-model:checked="form.privacy_mode" />
            <span class="hint">{{ $t('开启后转发 Python 引擎时注入 X-Privacy-Mode: no_retention，不落库历史') }}</span>
          </a-form-item>
          <a-form-item :label="$t('数据留存天数（0 = 永久）')">
            <a-input-number
              v-model:value="form.data_retention_days"
              :min="0"
              style="width: 200px"
            />
          </a-form-item>
          <a-form-item :label="$t('允许训练')">
            <a-switch v-model:checked="form.training_allowed" />
            <span class="hint">{{ $t('关闭后该租户内容不得用于模型训练') }}</span>
          </a-form-item>
          <a-form-item :label="$t('脱敏规则（JSON）')">
            <a-textarea
              v-model:value="form.redaction_rules"
              :rows="8"
              class="code-editor"
              placeholder="{&quot;phone&quot;: &quot;regex&quot;, &quot;email&quot;: &quot;regex&quot;}"
            />
          </a-form-item>
          <a-form-item>
            <a-button
              type="primary"
              :loading="saving"
              @click="save"
            >
              {{ $t('保存') }}
            </a-button>
          </a-form-item>
        </a-form>
      </a-card>
    </a-spin>
  </div>
</template>

<style scoped>
.privacy-view { padding: 16px 24px; }
.page-header { margin-bottom: 16px; }
.page-title { margin: 0; font-size: 20px; }
.hint { margin-left: 12px; color: var(--text-secondary); font-size: 12px; }

/* JSON 编辑区:等宽字体、min-height 自适应、可纵向拉伸 */
.privacy-view :deep(.code-editor) {
  font-family: var(--font-mono);
  font-size: 13px;
  min-height: 160px;
  resize: vertical;
}

/* 窄屏:表单项全宽、提示换行、触控目标 ≥ 40px */
@media (max-width: 768px) {
  .privacy-view :deep(.ant-input-number) { width: 100% !important; }
  .privacy-view .hint { display: block; margin-left: 0; margin-top: 4px; }
  .privacy-view .ant-btn { min-height: 40px; }
}
</style>
