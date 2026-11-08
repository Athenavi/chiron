<script setup lang="ts">
/**
 * 租户后台 · 站点设置。
 *
 * 配置本租户的品牌：页面标题、站点图标（URL）与描述。保存后：
 *   - 当前页面立即应用标题/图标/描述；
 *   - 该租户所有登录用户下次进入时由 AppLayout 读取并应用。
 *
 * 未配置时保持平台默认品牌（using_default=true）。
 */
import { onMounted, reactive, ref } from 'vue'
import { Alert, Button, Card, Form, FormItem, Input, message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { getTenantSiteConfig, saveTenantSiteConfig, uploadSiteIcon } from '../../api/admin'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()
const loading = ref(false)
const saving = ref(false)
const uploading = ref(false)
const usingDefault = ref(true)
const iconInput = ref<HTMLInputElement>()
const form = reactive({ title: '', icon_url: '', description: '' })

/** 选择本地图片 → 上传到后端 → 回填可直接访问的图标 URL。 */
function pickIcon() {
  iconInput.value?.click()
}

async function onIconChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (!['.png', '.jpg', '.jpeg', '.webp', '.ico'].includes(ext)) {
    message.warning(t('admin.site_icon_type_invalid'))
    return
  }
  if (file.size > 2 * 1024 * 1024) {
    message.warning(t('admin.site_icon_too_large'))
    return
  }
  uploading.value = true
  try {
    const url = await uploadSiteIcon(file)
    form.icon_url = url
    message.success(t('admin.site_icon_uploaded'))
  } catch (e) {
    message.error(serverErrorMessage(e, t('admin.site_icon_upload_failed')))
  } finally {
    uploading.value = false
  }
}

async function load() {
  loading.value = true
  try {
    const res = await getTenantSiteConfig()
    Object.assign(form, res.config)
    usingDefault.value = res.using_default
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.failed_to_load')))
  } finally {
    loading.value = false
  }
}

/** 与 AppLayout 应用逻辑保持一致：保存后立刻在当前页看到效果。 */
function applyToDocument() {
  if (form.title) document.title = form.title
  if (form.description) {
    let meta = document.querySelector('meta[name="description"]')
    if (!meta) {
      meta = document.createElement('meta')
      meta.setAttribute('name', 'description')
      document.head.appendChild(meta)
    }
    meta.setAttribute('content', form.description)
  }
  if (form.icon_url) {
    let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')
    if (!link) {
      link = document.createElement('link')
      link.setAttribute('rel', 'icon')
      document.head.appendChild(link)
    }
    link.href = form.icon_url
  }
}

async function save() {
  saving.value = true
  try {
    await saveTenantSiteConfig({ ...form })
    usingDefault.value = !form.title && !form.icon_url && !form.description
    applyToDocument()
    message.success(t('common.saved'))
  } catch (e) {
    message.error(serverErrorMessage(e, t('common.save_failed')))
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="site-settings">
    <div class="site-header">
      <h2 class="page-title">
        {{ $t('admin.site_settings') }}
      </h2>
      <p class="page-desc">
        {{ $t('admin.site_settings_desc') }}
      </p>
    </div>

    <Alert
      v-if="usingDefault"
      type="info"
      show-icon
      :closable="false"
      class="hint"
      :message="$t('admin.site_using_default')"
    />

    <Card :loading="loading">
      <Form
        layout="vertical"
        class="site-form"
      >
        <FormItem :label="$t('admin.site_title')">
          <Input
            v-model:value="form.title"
            :maxlength="128"
            :placeholder="$t('admin.site_title_placeholder')"
          />
        </FormItem>
        <FormItem :label="$t('admin.site_icon_url')">
          <div class="icon-row">
            <Input
              v-model:value="form.icon_url"
              placeholder="https://cdn.example.com/icon.png"
              allow-clear
            />
            <Button
              :loading="uploading"
              @click="pickIcon"
            >
              {{ $t('admin.site_icon_upload') }}
            </Button>
            <input
              ref="iconInput"
              type="file"
              accept="image/png,image/jpeg,image/webp,image/x-icon"
              hidden
              @change="onIconChange"
            >
          </div>
          <span class="hint-text">{{ $t('admin.site_icon_hint') }}</span>
        </FormItem>
        <FormItem :label="$t('admin.site_description')">
          <Input.TextArea
            v-model:value="form.description"
            :maxlength="512"
            :rows="3"
            show-count
          />
        </FormItem>
        <Button
          type="primary"
          :loading="saving"
          @click="save"
        >
          {{ $t('common.save') }}
        </Button>
      </Form>
    </Card>
  </div>
</template>

<style scoped>
.site-settings { padding: 4px; }
.site-header { margin-bottom: 12px; }
.page-title { margin: 0; font-size: 18px; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-tertiary); }
.hint { margin-bottom: 12px; }
.site-form { max-width: 560px; }
.hint-text { display: block; margin-top: 4px; font-size: 12px; color: var(--text-tertiary); }
.icon-row { display: flex; gap: 8px; align-items: center; }
</style>
