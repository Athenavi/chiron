<script setup lang="ts">
/**
 * S3 / MinIO 对象存储配置（数据存储 → S3 tab）。
 *
 * 与「系统设置」里的存储卡片同源（/v1/admin/storage），但这里把对象存储
 * 当作数据存储的一等公民：显式展示当前生效后端、允许切换并测试连通性。
 *
 * 长期凭据（access / secret）**不回显**：留空 = 沿用已保存值（后端补齐）。
 * 否则每次打开页面都能在 DOM 里读到 secret，等于把凭据发给每个能进后台的人。
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  Alert,
  Button,
  Card,
  Form,
  FormItem,
  Input,
  Select,
  Space,
  Switch,
  message,
} from 'ant-design-vue'
import { getStorage, testStorage, updateStorage } from '../../api/admin'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const backend = ref<'local' | 's3'>('local')
const endpoint = ref('')
const bucket = ref('')
const accessKey = ref('')
const secretKey = ref('')
const useSSL = ref(true)

const isS3 = computed(() => backend.value === 's3')

const backendOptions = computed(() => [
  { value: 'local', label: t('admin.s3_backend_local') },
  { value: 's3', label: 'S3 / MinIO' },
])

async function load() {
  loading.value = true
  try {
    const s = await getStorage()
    backend.value = s.backend === 's3' ? 's3' : 'local'
    const cfg = (s.config ?? {}) as Record<string, unknown>
    endpoint.value = typeof cfg.s3_endpoint === 'string' ? cfg.s3_endpoint : ''
    bucket.value = typeof cfg.s3_bucket === 'string' ? cfg.s3_bucket : ''
    useSSL.value = cfg.s3_use_ssl !== false
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.failed_to_load')))
  } finally {
    loading.value = false
  }
}

function payload() {
  return {
    backend: backend.value,
    s3_endpoint: endpoint.value.trim(),
    s3_bucket: bucket.value.trim(),
    s3_access_key: accessKey.value.trim(),
    s3_secret_key: secretKey.value.trim(),
    s3_use_ssl: useSSL.value,
  }
}

async function save() {
  saving.value = true
  try {
    const res = await updateStorage(payload())
    message.success(t('common.storage_config_saved'))
    if (res.warning) message.warning(res.warning)
    // 保存成功后清空凭据输入框：它们不会再被回显，避免误以为页面上显示的是已保存值。
    accessKey.value = ''
    secretKey.value = ''
    await load()
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.save_failed')))
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  try {
    const res = await testStorage(payload())
    if (res.status === 'ok') message.success(res.message || t('common.test_connection'))
    else message.error(res.message || t('common.test_connection'))
  } catch (e) {
    message.error(serverErrorMessage(e, t('common.test_connection')))
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <Card
    :title="t('common.object_storage_s3_minio_config')"
    :loading="loading"
  >
    <Alert
      type="info"
      show-icon
      :closable="false"
      class="s3-hint"
      :message="t('admin.s3_credentials_keep_hint')"
    />
    <Form
      layout="vertical"
      class="s3-form"
    >
      <FormItem :label="t('common.backend_type')">
        <Select
          v-model:value="backend"
          :options="backendOptions"
          style="width: 220px"
        />
      </FormItem>
      <template v-if="isS3">
        <FormItem :label="t('admin.s3_endpoint')">
          <Input
            v-model:value="endpoint"
            placeholder="https://s3.example.com"
            autocomplete="off"
          />
        </FormItem>
        <FormItem :label="t('admin.s3_bucket')">
          <Input
            v-model:value="bucket"
            placeholder="chiron"
            autocomplete="off"
          />
        </FormItem>
        <FormItem :label="t('admin.s3_access_key')">
          <Input
            v-model:value="accessKey"
            autocomplete="off"
          />
        </FormItem>
        <FormItem :label="t('admin.s3_secret_key')">
          <Input.Password
            v-model:value="secretKey"
            autocomplete="new-password"
          />
        </FormItem>
        <FormItem :label="t('admin.s3_use_ssl')">
          <Switch v-model:checked="useSSL" />
        </FormItem>
      </template>
      <Space>
        <Button
          type="primary"
          :loading="saving"
          @click="save"
        >
          {{ t('common.save') }}
        </Button>
        <Button
          v-if="isS3"
          :loading="testing"
          @click="test"
        >
          {{ t('common.test_connection') }}
        </Button>
      </Space>
    </Form>
  </Card>
</template>

<style scoped>
.s3-hint { margin-bottom: 16px; }
.s3-form { max-width: 560px; }
</style>
