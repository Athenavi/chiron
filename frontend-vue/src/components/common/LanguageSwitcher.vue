<script setup lang="ts">
/**
 * 语言切换器。
 *
 * 显示各语言的**母语名**（简体中文 / English / العربية）而不是当前界面语言的译名 ——
 * 用户在看不懂当前界面语言时，仍能找到自己的语言。
 *
 * 切换动作由 setLocale() 统一处理：写 localStorage、更新 <html lang/dir>、
 * 同步 dayjs；antd 组件库语言经 App.vue 的 ConfigProvider 自动跟随。
 *
 * 两种形态：
 *  - 默认（下拉选择）：顶栏、设置面板、登录/注册等表单页 —— 空间充足，当前语言直接可见；
 *  - `compact`（地球图标 + 菜单）：聊天页侧栏底部这种一行要塞头像 / 名字 / 主题 / 菜单的
 *    地方，再放一个 7.5rem 宽的选择器会把用户名挤没。
 */
import { computed } from 'vue'
import { Dropdown, Menu } from 'ant-design-vue'
import { GlobalOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { LANGUAGES } from '../../i18n/languages'
import { setLocale } from '../../i18n'

const props = withDefaults(defineProps<{ compact?: boolean }>(), { compact: false })

const { t, locale } = useI18n()

const options = LANGUAGES.map(l => ({ value: l.code, label: l.nativeName }))
const menuItems = LANGUAGES.map(l => ({ key: l.code, label: l.nativeName }))

/** 当前语言的母语名：compact 形态用它做 title / aria-label（图标本身不显示语言） */
const currentNativeName = computed(
  () => LANGUAGES.find(l => l.code === locale.value)?.nativeName ?? LANGUAGES[0].nativeName,
)

function onChange(value: unknown): void {
  setLocale(String(value))
}

function onMenuClick(e: { key: string | number }): void {
  setLocale(String(e.key))
}
</script>

<template>
  <Dropdown
    v-if="props.compact"
    trigger="click"
    placement="topRight"
  >
    <button
      type="button"
      class="lang-compact-btn"
      :title="`${t('common.language')}：${currentNativeName}`"
      :aria-label="`${t('common.language')}：${currentNativeName}`"
    >
      <GlobalOutlined />
    </button>
    <template #overlay>
      <Menu
        :items="menuItems"
        :selected-keys="[String(locale)]"
        @click="onMenuClick"
      />
    </template>
  </Dropdown>
  <a-select
    v-else
    :value="locale"
    :options="options"
    :aria-label="t('common.language')"
    size="small"
    class="language-switcher"
    @change="onChange"
  />
</template>

<style scoped>
/* 逻辑属性：min-inline-size 在 RTL 下同样按"行内方向"生效，避免物理宽度假设 */
.language-switcher {
  min-inline-size: 7.5rem;
}

/* compact：与 ChatSidePanel 的 .foot-user-btn 同尺寸同色，视觉上属于同一组图标按钮 */
.lang-compact-btn {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm, 4px);
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
}
.lang-compact-btn:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}
</style>
