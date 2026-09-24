<script setup lang="ts">
import { CodeOutlined, EditOutlined, BarChartOutlined, BulbOutlined } from '@ant-design/icons-vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const emit = defineEmits<{ (e: 'suggest', text: string): void }>()

// 图标沿用产品其余部分的 ant-design 图标集，避免 emoji 在深浅主题下渲染不一致
const suggestions = [
  { icon: CodeOutlined, title: t('代码生成'), desc: t('写一段 Python 代码实现排序算法'), prompt: t('写一段 Python 代码实现排序算法') },
  { icon: EditOutlined, title: t('创意写作'), desc: t('帮我写一篇关于 AI 的短文'), prompt: t('帮我写一篇关于 AI 的短文') },
  { icon: BarChartOutlined, title: t('数据分析'), desc: t('分析这份数据的趋势'), prompt: t('分析这份数据的趋势') },
  { icon: BulbOutlined, title: t('方案策划'), desc: t('帮我做一个项目计划'), prompt: t('帮我做一个项目计划') },
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
        {{ $t('你好，有什么可以帮助你的？') }}
      </h1>
<!--      <div class="suggestion-grid">-->
<!--        <div-->
<!--          v-for="s in suggestions"-->
<!--          :key="s.title"-->
<!--          class="suggestion-card"-->
<!--          role="button"-->
<!--          tabindex="0"-->
<!--          :aria-label="s.prompt"-->
<!--          @click="emit('suggest', s.prompt)"-->
<!--          @keydown.enter.prevent="emit('suggest', s.prompt)"-->
<!--          @keydown.space.prevent="emit('suggest', s.prompt)"-->
<!--        >-->
<!--          <component-->
<!--            :is="s.icon"-->
<!--            class="card-icon"-->
<!--          />-->
<!--          <div class="card-title">-->
<!--            {{ s.title }}-->
<!--          </div>-->
<!--          <div class="card-desc">-->
<!--            {{ s.desc }}-->
<!--          </div>-->
<!--        </div>-->
<!--      </div>-->
    </div>
  </div>
</template>

<style scoped>
.hero { position: relative; flex: 1; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.hero-glow { position: absolute; inset: -40% -20% auto -20%; height: 70%; background: radial-gradient(ellipse 50% 50% at 50% 0%, var(--primary-bg), transparent 70%); pointer-events: none; }
.hero-content { width: 560px; max-width: calc(100vw - 48px); text-align: center; position: relative; z-index: var(--z-content); }
.hero-logo { width: 56px; height: 56px; margin: 0 auto 18px; border-radius: var(--sig-radius-card); background: linear-gradient(135deg, var(--primary), var(--accent)); color: var(--on-solid); font-weight: 700; font-size: 20px; display: flex; align-items: center; justify-content: center; box-shadow: var(--sig-shadow-hover); }
.hero-title { font-size: 24px; font-weight: 650; color: var(--text-primary); letter-spacing: -0.01em; margin-bottom: 28px; }
.suggestion-grid { display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; }
.suggestion-card { width: 250px; padding: 14px; border-radius: var(--sig-radius-card); border: 1px solid var(--border-card); background: var(--bg-card); cursor: pointer; transition: all var(--dur-normal); text-align: left; box-shadow: var(--sig-shadow-card); }
.suggestion-card:hover { border-color: var(--primary); transform: translateY(-2px); box-shadow: var(--sig-shadow-hover); }
.card-icon { display: block; font-size: 18px; line-height: 1; color: var(--text-secondary); margin-bottom: 8px; transition: color var(--dur-normal); }
.suggestion-card:hover .card-icon { color: var(--primary); }
.card-title { font-size: 14px; font-weight: 600; color: var(--text-primary); margin-bottom: 4px; }
.card-desc { font-size: 12px; color: var(--text-tertiary); line-height: 1.4; }
@media (max-width: 768px) { .suggestion-card { width: 100%; } .hero-content { width: 100%; } }
/* ── 微交互 + 可访问性：键盘可操作、焦点可见、按压反馈 ── */
.suggestion-card:active { transform: scale(0.98); border-color: var(--primary); }
.suggestion-card:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
/* ── 移动端：窄屏密度压缩 ── */
@media (max-width: 576px) {
  .hero-content { padding: 0 16px; }
  .hero-logo { width: 48px; height: 48px; font-size: 18px; border-radius: var(--sig-radius-button); margin-bottom: 14px; }
  .hero-title { font-size: 20px; margin-bottom: 20px; }
  .suggestion-grid { gap: 8px; }
  .suggestion-card { padding: 12px; }
}
</style>
