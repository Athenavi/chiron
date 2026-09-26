<script setup lang="ts">
import { Modal, Button, Segmented } from 'ant-design-vue'
import {
  CONTENT_WIDTH_OPTIONS,
  GAP_OPTIONS,
  LEADING_OPTIONS,
  TEXT_SIZE_OPTIONS,
  useTypographyStore,
} from '../../stores/typography'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const typography = useTypographyStore()
</script>

<template>
  <Modal
    :open="open"
    :title="$t('settings.display_settings')"
    :footer="null"
    width="420px"
    @cancel="emit('update:open', false)"
  >
    <div class="display-row">
      <span class="display-label">{{ $t('common.body_font_size') }}</span>
      <Segmented
        :value="typography.state.textSize"
        :options="TEXT_SIZE_OPTIONS"
        @change="(v: any) => typography.setTextSize(Number(v))"
      />
    </div>
    <div class="display-row">
      <span class="display-label">{{ $t('common.line_spacing') }}</span>
      <Segmented
        :value="typography.state.leading"
        :options="LEADING_OPTIONS"
        @change="(v: any) => typography.setLeading(Number(v))"
      />
    </div>
    <div class="display-row">
      <span class="display-label">{{ $t('chat.message_spacing') }}</span>
      <Segmented
        :value="typography.state.gap"
        :options="GAP_OPTIONS"
        @change="(v: any) => typography.setGap(Number(v))"
      />
    </div>
    <div class="display-row">
      <span class="display-label">{{ $t('common.content_width') }}</span>
      <Segmented
        :value="typography.state.contentWidth"
        :options="CONTENT_WIDTH_OPTIONS"
        @change="(v: any) => typography.setContentWidth(Number(v))"
      />
    </div>
    <div class="display-foot">
      <span class="display-hint">{{ $t('common.only_affects_local_reading_presentation_saved_in_the_browser_locally') }}</span>
      <Button
        size="small"
        @click="typography.reset()"
      >
        {{ $t('common.restore_defaults') }}
      </Button>
    </div>
  </Modal>
</template>

<style scoped>
.display-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 0; }
.display-label { font-size: 13px; color: var(--text-secondary); }
.display-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border-subtle); }
.display-hint { font-size: 11px; color: var(--text-tertiary); }
</style>
