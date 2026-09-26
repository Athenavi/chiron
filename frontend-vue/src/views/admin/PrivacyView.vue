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
    message.error(e?.response?.data?.error || t('errors.failed_to_load'))
  } finally {
    loading.value = false
  }
}

async function save() {
  let rules: unknown
  try {
    rules = JSON.parse(form.value.redaction_rules || '{}')
  } catch {
    message.error(t('common.desensitization_rules_must_be_valid_json'))
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
    message.success(t('common.saved'))
    fetchPrivacy()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.save_failed'))
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
        {{ $t('admin.privacy_mode_control') }}
      </h2>
    </div>

    <a-spin :spinning="loading">
      <a-card
        :title="$t('admin.tenant_privacy_policy')"
        style="max-width: 720px"
      >
        <a-form layout="vertical">
          <a-form-item :label="$t('admin.privacy_mode')">
            <a-switch v-model:checked="form.privacy_mode" />
            <span class="hint">{{ $t('admin.when_enabled_forwards_to_the_python_engine_with_x_privacy_mode_no_retention_so_history_is_not_stored') }}</span>
          </a-form-item>
          <a-form-item :label="$t('common.data_retention_days_0_permanent')">
            <a-input-number
              v-model:value="form.data_retention_days"
              :min="0"
              style="width: 200px"
            />
          </a-form-item>
          <a-form-item :label="$t('common.allow_training')">
            <a-switch v-model:checked="form.training_allowed" />
            <span class="hint">{{ $t('agent.after_disabling_this_tenant_s_content_must_not_be_used_for_model_training') }}</span>
          </a-form-item>
          <a-form-item :label="$t('common.desensitization_rules_json')">
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
              {{ $t('common.save') }}
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
