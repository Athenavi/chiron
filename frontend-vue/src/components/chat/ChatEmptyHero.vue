<script setup lang="ts">
import { CodeOutlined, EditOutlined, BarChartOutlined, BulbOutlined } from '@ant-design/icons-vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const emit = defineEmits<{ (e: 'suggest', text: string): void }>()

// 图标沿用产品其余部分的 ant-design 图标集，避免 emoji 在深浅主题下渲染不一致
const suggestions = [
  { icon: CodeOutlined, title: t('common.code_generation'), desc: t('common.write_a_python_snippet_implementing_a_sorting_algorithm'), prompt: t('common.write_a_python_snippet_implementing_a_sorting_algorithm') },
  { icon: EditOutlined, title: t('common.creative_writing'), desc: t('common.write_me_a_short_essay_about_ai'), prompt: t('common.write_me_a_short_essay_about_ai') },
  { icon: BarChartOutlined, title: t('common.data_analysis'), desc: t('common.analyze_the_trend_in_this_data'), prompt: t('common.analyze_the_trend_in_this_data') },
  { icon: BulbOutlined, title: t('billing.planning'), desc: t('billing.help_me_make_a_project_plan'), prompt: t('billing.help_me_make_a_project_plan') },
]
</script>

<template>
  <div class="hero">
    <div
      class="hero-glow"
      aria-hidden
    />
    <div class="hero-content">
      <div class="hero-logo">
        MC
      </div>
      <h1 class="hero-title">
        {{ $t('common.hello_how_can_i_help_you') }}
      </h1>
      <!-- 建议卡：这里沿用 role="button" + tabindex + 键盘事件（a11y 契约把本处列为范例），
           不改成 <button> —— 卡片里是多行排版，按钮的默认行内行为反而要额外覆盖。 -->
      <div class="suggestion-grid">
        <div
          v-for="s in suggestions"
          :key="s.title"
          class="suggestion-card"
          role="button"
          tabindex="0"
          :aria-label="s.prompt"
          @click="emit('suggest', s.prompt)"
          @keydown.enter.prevent="emit('suggest', s.prompt)"
          @keydown.space.prevent="emit('suggest', s.prompt)"
        >
          <component
            :is="s.icon"
            class="card-icon"
          />
          <div class="card-title">
            {{ s.title }}
          </div>
          <div class="card-desc">
            {{ s.desc }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.hero { position: relative; flex: 1; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.hero-glow { position: absolute; inset: -40% -20% auto -20%; height: 70%; background: radial-gradient(ellipse 50% 50% at 50% 0%, var(--primary-bg), transparent 70%); pointer-events: none; }
.hero-content { width: 560px; max-width: calc(100vw - 48px); text-align: center; position: relative; z-index: var(--z-content); }
.hero-logo { width: 56px; height: 56px; margin: 0 auto 18px; border-radius: var(--sig-radius-card); background: linear-gradient(135deg, var(--primary), var(--accent)); color: var(--on-solid); font-weight: var(--fw-bold); font-size: 20px; display: flex; align-items: center; justify-content: center; box-shadow: var(--sig-shadow-hover); }
/* 问候语走 CDS 的「衬线版面标题」（.font-display）：衬线 + 38px + 轻字重 + 微负字距。
   字号由 --fs-display 派生，仍受"界面字号只有一个入口"的约束。 */
.hero-title { font-family: var(--font-serif); font-size: var(--fs-display); font-weight: var(--fw-light); line-height: 1.2; letter-spacing: -0.015em; color: var(--text-primary); margin-bottom: 28px; }
.suggestion-grid { display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; }
.suggestion-card { width: 250px; padding: 14px; border-radius: var(--sig-radius-card); border: var(--hairline-width) solid var(--border-card); background: var(--bg-card); cursor: pointer; transition: all var(--dur-normal); text-align: left; box-shadow: var(--sig-shadow-card); }
.suggestion-card:hover { border-color: var(--primary); transform: translateY(-2px); box-shadow: var(--sig-shadow-hover); }
.card-icon { display: block; font-size: 18px; line-height: 1; color: var(--text-secondary); margin-bottom: 8px; transition: color var(--dur-normal); }
.suggestion-card:hover .card-icon { color: var(--primary); }
.card-title { font-size: 14px; font-weight: var(--fw-semibold); color: var(--text-primary); margin-bottom: 4px; }
.card-desc { font-size: 12px; color: var(--text-tertiary); line-height: 1.4; }
@media (max-width: 768px) { .suggestion-card { width: 100%; } .hero-content { width: 100%; } }
/* ── 微交互 + 可访问性：键盘可操作、焦点可见、按压反馈 ── */
.suggestion-card:active { transform: scale(0.98); border-color: var(--primary); }
.suggestion-card:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: var(--focus-ring-offset); }
/* ── 移动端：窄屏密度压缩（展示级字号降两档，否则一行放不下） ── */
@media (max-width: 576px) {
  .hero-content { padding: 0 16px; }
  .hero-logo { width: 48px; height: 48px; font-size: 18px; border-radius: var(--sig-radius-button); margin-bottom: 14px; }
  .hero-title { font-size: var(--fs-3xl); margin-bottom: 20px; }
  .suggestion-grid { gap: 8px; }
  .suggestion-card { padding: 12px; }
}
</style>
