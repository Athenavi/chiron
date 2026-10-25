<script setup lang="ts">
/**
 * 租户 / 用户级 harness 塑形配置（P1-④ 批 B）。
 *
 * * `scope="user"` —— 写自己的（`/v1/settings/harness-profiles`，鉴权只需登录）；
 * * `scope="tenant"` —— 租户级默认（`/v1/admin/settings/harness-profiles`，需 PermPolicyManage）。
 *
 * **这不是普通偏好项**：配置会被追加到 system prompt 的末尾，直接改变模型行为。
 * 因此页面上必须**显著告知**（`Alert` 常驻，不是 tooltip）—— 见下面的 harley_warning_*。
 */
import { computed, onMounted, ref } from 'vue'
import { Button, Empty, Input, Popconfirm, Switch, Tag, message } from 'ant-design-vue'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { api } from '../../api'

const props = withDefaults(defineProps<{ scope?: 'user' | 'tenant' }>(), { scope: 'user' })
const { t } = useI18n()

type Profile = {
  model_prefix: string
  system_prompt_suffix: string
  enabled: boolean
  source?: string
}

const endpoint = computed(() =>
  props.scope === 'tenant' ? '/v1/admin/settings/harness-profiles' : '/v1/settings/harness-profiles',
)

const loading = ref(false)
const saving = ref(false)
const rows = ref<Profile[]>([])
const effective = ref<Profile[]>([])
const maxChars = ref(2000)
const draft = ref<Profile>({ model_prefix: '', system_prompt_suffix: '', enabled: true })

const suffixLength = computed(() => [...draft.value.system_prompt_suffix].length)
const tooLong = computed(() => suffixLength.value > maxChars.value)

function fail(error: unknown, fallback: string) {
  message.error((error instanceof Error ? error.message : '') || fallback)
}

async function load() {
  loading.value = true
  try {
    const resp = await api.get(endpoint.value)
    const data = resp.data?.data ?? {}
    rows.value = (props.scope === 'tenant' ? data.tenant_defaults : data.user_overrides) ?? []
    effective.value = data.effective ?? []
    maxChars.value = data.limits?.max_suffix_chars ?? 2000
  } catch (error) {
    fail(error, t('settings.harness_load_failed'))
  } finally {
    loading.value = false
  }
}

async function save() {
  if (tooLong.value) return
  saving.value = true
  try {
    const body = {
      model_prefix: draft.value.model_prefix.trim().toLowerCase(),
      system_prompt_suffix: draft.value.system_prompt_suffix,
      enabled: draft.value.enabled,
    }
    await api.put(endpoint.value, body)
    message.success(t('settings.harness_saved'))
    draft.value = { model_prefix: '', system_prompt_suffix: '', enabled: true }
    await load()
  } catch (error) {
    fail(error, t('settings.harness_save_failed'))
  } finally {
    saving.value = false
  }
}

async function remove(prefix: string) {
  try {
    await api.delete(`${endpoint.value}/${encodeURIComponent(prefix)}`)
    message.success(t('settings.harness_deleted'))
    await load()
  } catch (error) {
    fail(error, t('settings.harness_delete_failed'))
  }
}

function edit(row: Profile) {
  draft.value = { ...row }
}

onMounted(load)
</script>

<template>
  <div class="harness-panel">
    <!-- 告知：常驻 Alert，不是 tooltip —— 这是会改变模型行为的配置 -->
    <a-alert
      type="warning"
      show-icon
      class="harness-alert"
      :message="$t('settings.harness_warning_title')"
      :description="$t('settings.harness_warning_body')"
    />

    <div class="setting-block">
      <div class="setting-title">
        {{ $t('settings.harness_add') }}
        <span class="setting-hint">{{ $t('settings.harness_limit', { max: maxChars }) }}</span>
      </div>

      <div class="harness-form">
        <Input
          v-model:value="draft.model_prefix"
          class="harness-prefix"
          :placeholder="$t('settings.harness_prefix_placeholder')"
        />
        <Input.TextArea
          v-model:value="draft.system_prompt_suffix"
          :rows="4"
          :placeholder="$t('settings.harness_suffix_placeholder')"
        />
        <div class="harness-row">
          <Switch v-model:checked="draft.enabled" />
          <span class="harness-hint">{{ $t('settings.harness_enabled_hint') }}</span>
          <span class="harness-count" :class="{ over: tooLong }">
            {{ suffixLength }} / {{ maxChars }}
          </span>
          <Button type="primary" :loading="saving" :disabled="tooLong" @click="save">
            <PlusOutlined /> {{ $t('settings.harness_save') }}
          </Button>
        </div>
      </div>
    </div>

    <div class="setting-block">
      <div class="setting-title">{{ $t('settings.harness_configured') }}</div>
      <a-spin :spinning="loading">
        <Empty v-if="!rows.length" :description="$t('settings.harness_empty')" />
        <div v-for="row in rows" :key="row.model_prefix" class="harness-item">
          <div class="harness-item-head">
            <Tag>{{ row.model_prefix || $t('settings.harness_all_models') }}</Tag>
            <Tag v-if="!row.enabled" color="default">{{ $t('settings.harness_disabled') }}</Tag>
            <span class="harness-item-actions">
              <button
                type="button"
                class="link-btn"
                @click="edit(row)"
              >
                {{ $t('settings.harness_edit') }}
              </button>
              <Popconfirm :title="$t('settings.harness_confirm_delete')" @confirm="remove(row.model_prefix)">
                <button
                  type="button"
                  class="link-btn"
                  :aria-label="$t('common.delete')"
                >
                  <DeleteOutlined />
                </button>
              </Popconfirm>
            </span>
          </div>
          <div class="harness-item-body">{{ row.system_prompt_suffix }}</div>
        </div>
      </a-spin>
    </div>

    <div v-if="scope === 'user' && effective.length" class="setting-block">
      <div class="setting-title">
        {{ $t('settings.harness_effective') }}
        <span class="setting-hint">{{ $t('settings.harness_effective_hint') }}</span>
      </div>
      <div v-for="row in effective" :key="`eff-${row.model_prefix}`" class="harness-item">
        <div class="harness-item-head">
          <Tag>{{ row.model_prefix || $t('settings.harness_all_models') }}</Tag>
          <Tag :color="row.source === 'user' ? 'blue' : 'green'">
            {{ row.source === 'user' ? $t('settings.harness_from_user') : $t('settings.harness_from_tenant') }}
          </Tag>
        </div>
        <div class="harness-item-body">{{ row.enabled ? row.system_prompt_suffix : $t('settings.harness_disabled') }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.harness-alert { margin-bottom: 16px; }
.harness-form { display: flex; flex-direction: column; gap: 10px; }
.harness-prefix { max-width: 320px; }
.harness-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.harness-hint { color: var(--text-secondary); font-size: 12px; }
.harness-count { margin-inline-start: auto; font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.harness-count.over { color: var(--error, #ff4d4f); font-weight: var(--fw-semibold); }
.harness-item { padding: 10px 0; border-bottom: 1px dashed var(--border-color, #f0f0f0); }
.harness-item-head { display: flex; align-items: center; gap: 8px; }
.harness-item-actions { margin-inline-start: auto; display: flex; gap: 12px; }
/* 语义化按钮而非 <a>：键盘可聚焦（check-a11y 的 clickable-nonsemantic 契约） */
.link-btn {
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  color: var(--primary);
  cursor: pointer;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.link-btn:hover { color: var(--primary); }
.harness-item-body { margin-top: 6px; white-space: pre-wrap; font-size: 13px; color: var(--text-secondary); }
</style>
