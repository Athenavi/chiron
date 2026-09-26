<script setup lang="ts">
import { markRaw, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button } from 'ant-design-vue'
import {
  MessageOutlined, UserOutlined, ApartmentOutlined, BlockOutlined,
  BookOutlined, ThunderboltOutlined, ArrowRightOutlined, ArrowUpOutlined,
} from '@ant-design/icons-vue'
import HomeScene3D from '../components/home/HomeScene3D.vue'
import WorkstationNav from '../components/WorkstationNav.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const router = useRouter()

interface Feature {
  title: string
  en: string
  desc: string
  path: string
  icon: any
}

// 图标组件需 markRaw：避免被 Vue 响应式代理（图标是静态组件，代理会破坏渲染）
const features: Feature[] = [
  { title: t('chat.chat'), en: 'CHAT', desc: t('agent.normal_minimal_ptc_creative_modes_tool_calls_are_fully_visualized'), path: '/chat', icon: markRaw(MessageOutlined) },
  { title: 'Agent', en: 'AGENTS', desc: t('workflow.multi_agent_collaboration_task_dispatch_and_result_tracking'), path: '/agents', icon: markRaw(UserOutlined) },
  { title: t('workflow.workflow'), en: 'WORKFLOW', desc: t('workflow.visually_orchestrate_multi_step_tasks_with_freely_connected_nodes'), path: '/workflow', icon: markRaw(ApartmentOutlined) },
  { title: t('agent.skill'), en: 'SKILLS', desc: t('agent.pluggable_skill_marketplace_load_and_unload_on_demand'), path: '/skills', icon: markRaw(BlockOutlined) },
  { title: t('knowledge.knowledge_base'), en: 'KNOWLEDGE', desc: t('agent.document_ingestion_and_vector_retrieval_give_agents_a_solid_basis'), path: '/knowledge', icon: markRaw(BookOutlined) },
  { title: t('common.plugin'), en: 'PLUGINS', desc: t('agent.mcp_service_config_extends_the_agent_s_capability_boundary'), path: '/plugins', icon: markRaw(ThunderboltOutlined) },
]

const QUICKSTART_CMD = 'docker compose up -d postgres redis\ncp .env.example .env\npython run.py start'

const copied = ref(false)

async function copyQuickstart() {
  try {
    await navigator.clipboard.writeText(QUICKSTART_CMD)
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
  } catch { /* clipboard unavailable */ }
}

function scrollToFeatures() {
  document.getElementById('features')?.scrollIntoView({ behavior: 'smooth' })
}

// ── 特性卡片：IntersectionObserver 交错入场 + hover 3D tilt ──
// 触屏设备禁用 3D tilt 和 CTA 磁吸（无鼠标 hover 语义，且会触发误触抖动）
const isTouch = typeof window !== 'undefined'
  && ('ontouchstart' in window || navigator.maxTouchPoints > 0)

const cardEls = ref<(HTMLElement | null)[]>([])
let cardObserver: IntersectionObserver | null = null
let cardFallbackTimer: number | undefined

function setCardRef(el: HTMLElement | null, index: number) {
  cardEls.value[index] = el
}

function revealCards() {
  cardEls.value.forEach(el => el?.classList.add('visible'))
  cardObserver?.disconnect()
}

onMounted(() => {
  cardObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add('visible')
          cardObserver?.unobserve(entry.target)
        }
      }
    },
    { threshold: 0.12 },
  )
  cardEls.value.forEach(el => el && cardObserver?.observe(el))
  // 兜底：即使 IO 未触发（滚动容器异常等），2s 后强制显示所有卡片
  cardFallbackTimer = window.setTimeout(revealCards, 2000)
  window.addEventListener('scroll', onScroll, { passive: true })
})
onUnmounted(() => {
  cardObserver?.disconnect()
  if (cardFallbackTimer !== undefined) window.clearTimeout(cardFallbackTimer)
  window.removeEventListener('scroll', onScroll)
})

function onCardMove(e: MouseEvent) {
  if (isTouch) return
  const card = e.currentTarget as HTMLElement
  const r = card.getBoundingClientRect()
  const px = (e.clientX - r.left) / r.width - 0.5
  const py = (e.clientY - r.top) / r.height - 0.5
  card.style.transform =
    `perspective(600px) translateY(-3px) rotateX(${(-py * 6).toFixed(2)}deg) rotateY(${(px * 6).toFixed(2)}deg)`
}

function onCardLeave(e: MouseEvent) {
  const card = e.currentTarget as HTMLElement
  card.style.transform = ''
}

// ── CTA 磁吸：按钮轻微跟随鼠标 ──
function onCtaMove(e: MouseEvent) {
  if (isTouch) return
  const btn = e.currentTarget as HTMLElement
  const r = btn.getBoundingClientRect()
  const dx = (e.clientX - r.left - r.width / 2) * 0.12
  const dy = (e.clientY - r.top - r.height / 2) * 0.12
  btn.style.transform = `translate(${dx.toFixed(1)}px, ${dy.toFixed(1)}px)`
}

function onCtaLeave(e: MouseEvent) {
  const btn = e.currentTarget as HTMLElement
  btn.style.transform = ''
}

// ── 滚动到顶部按钮（长页面导航辅助） ──
const showTop = ref(false)
function onScroll() {
  showTop.value = window.scrollY > 600
}
function scrollToTop() {
  window.scrollTo({ top: 0, behavior: 'smooth' })
}
</script>

<template>
  <div class="home">
    <!-- Hero：Three.js 粒子场 + 渐变光晕 + 网格纹理 + 入场动画 -->
    <section class="hero">
      <div
        class="hero-glow"
        aria-hidden
      />
      <div
        class="hero-grid-bg"
        aria-hidden
      />
      <HomeScene3D />
      <div class="hero-content">
        <span class="hero-badge">
          <span class="hero-badge-dot" />{{ $t('agent.chiron_self_hosted_ai_agent_console') }}
        </span>
        <h1 class="hero-title">
          {{ $t('agent.let_the_agent') }}
          <span class="hero-title-accent">{{ $t('common.continuous_work') }}</span>
          <br>{{ $t('common.in_real_scenarios') }}
        </h1>
        <p class="hero-sub">
          {{ $t('workflow.chat_agent_workflow_skill_knowledge_base_and_plugins_unified_full_stack_capabilities_combine_freely') }}
          <br class="hero-br">{{ $t('common.traceable_visible_process_your_local_intelligent_workspace') }}
        </p>
        <div class="hero-actions">
          <Button
            type="primary"
            size="large"
            class="hero-cta glow"
            @mousemove="onCtaMove"
            @mouseleave="onCtaLeave"
            @click="router.push('/chat')"
          >
            {{ $t('chat.start_conversation') }}
            <ArrowRightOutlined />
          </Button>
          <Button
            size="large"
            class="hero-cta ghost"
            @mousemove="onCtaMove"
            @mouseleave="onCtaLeave"
            @click="scrollToFeatures"
          >
            {{ $t('common.browse_features') }}
          </Button>
        </div>
      </div>
    </section>

    <!-- 六大工作台统一入口：快速命令 + 工作台网格 + 最近活动（互联互通） -->
    <WorkstationNav />

    <section
      id="features"
      class="features"
    >
      <h2 class="section-title">
        {{ $t('common.six_capabilities_one_console') }}
      </h2>
      <p class="section-sub">
        {{ $t('common.each_capability_can_be_used_independently_or_freely_combined') }}
      </p>
      <div class="feature-grid">
        <div
          v-for="(f, i) in features"
          :key="f.title"
          :ref="(el: any) => setCardRef(el as HTMLElement | null, i)"
          class="feature-card"
          :style="{ '--card-delay': `${i * 60}ms` }"
          role="link"
          tabindex="0"
          @mousemove="onCardMove"
          @mouseleave="onCardLeave"
          @click="router.push(f.path)"
          @keydown.enter="router.push(f.path)"
        >
          <span class="feature-icon-wrap">
            <component
              :is="f.icon"
              class="feature-icon"
            />
          </span>
          <span class="feature-en">{{ f.en }}</span>
          <div class="feature-title">
            {{ f.title }}
          </div>
          <div class="feature-desc">
            {{ f.desc }}
          </div>
          <span class="feature-go">{{ $t('common.enter') }} <ArrowRightOutlined /></span>
        </div>
      </div>
    </section>

    <!-- 产品展示：真实工作台窗口预览（玻璃拟态） -->
    <section class="showcase">
      <h2 class="section-title">
        {{ $t('common.a_real_workbench_seen_through_at_a_glance') }}
      </h2>
      <p class="section-sub">
        {{ $t('agent.chat_trace_and_tool_calls_the_whole_process_is_visible') }}
      </p>
      <div class="showcase-grid">
        <!-- 窗口 1：对话界面 -->
        <div class="window-card">
          <div class="window-chrome">
            <span class="win-dot red" /><span class="win-dot yellow" /><span class="win-dot green" />
            <span class="win-title">{{ $t('chat.chiron_conversation') }}</span>
          </div>
          <div class="window-body chat-preview">
            <div class="pv-msg assistant">
              <div class="pv-bubble">
                {{ $t('workflow.i_will_help_you_analyze_this_data_first_let_me_break_the_requirements_into_a_few_steps') }}
              </div>
            </div>
            <div class="pv-msg user">
              <div class="pv-bubble user">
                {{ $t('common.please_use_python_to_generate_a_quarterly_trend_chart') }}
              </div>
            </div>
            <div class="pv-msg assistant">
              <div class="pv-tool">
                <span class="pv-tool-dot" />{{ $t('common.python_exec_running') }}
              </div>
              <div class="pv-bubble">
                {{ $t('common.trend_chart_generated_q2_up_23_qoq_code_and_chart_below') }}
              </div>
            </div>
            <div class="pv-input">
              <span>{{ $t('chat.send_message') }}</span>
            </div>
          </div>
        </div>
        <!-- 窗口 2：历史导航（轨迹 + 会话） -->
        <div class="window-card">
          <div class="window-chrome">
            <span class="win-dot red" /><span class="win-dot yellow" /><span class="win-dot green" />
            <span class="win-title">{{ $t('common.chiron_history_navigation') }}</span>
          </div>
          <div class="window-body panel-preview">
            <div class="pv-panel-head">
              <span class="pv-panel-title">{{ $t('chat.session_data_analysis') }}</span><span class="pv-panel-caret">▶</span>
            </div>
            <div class="pv-timeline">
              <div class="pv-timeline-track">
                <span
                  class="pv-span"
                  style="left: 0%; width: 26%"
                />
                <span
                  class="pv-span"
                  style="left: 34%; width: 18%"
                />
                <span
                  class="pv-span"
                  style="left: 60%; width: 32%"
                />
              </div>
            </div>
            <div class="pv-row">
              <span class="pv-dot" />{{ $t('common.analyze_the_trend_in_this_data') }}
            </div>
            <div class="pv-row active">
              <span class="pv-dot" />{{ $t('common.generate_quarterly_trend_chart') }}
            </div>
            <div class="pv-row">
              <span class="pv-dot" />{{ $t('common.compare_with_same_period_last_year') }}
            </div>
            <div class="pv-row">
              <span class="pv-dot" />{{ $t('common.summarize_as_weekly_report') }}
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- 快速开始：终端代码块 -->
    <section class="quickstart">
      <h2 class="section-title">
        {{ $t('common.quick_start') }}
      </h2>
      <p class="section-sub">
        {{ $t('common.one_command_to_start_dependencies_three_lines_to_enter_the_workspace') }}
      </p>
      <div class="terminal-card">
        <div class="window-chrome">
          <span class="win-dot red" /><span class="win-dot yellow" /><span class="win-dot green" />
          <span class="win-title">zsh · chiron</span>
          <button
            type="button"
            class="term-copy"
            @click="copyQuickstart"
          >
            {{ copied ? $t('common.copied_2') : $t('common.copy') }}
          </button>
        </div>
        <div class="terminal-body">
          <div class="term-line">
            <span class="term-prompt">$</span> docker compose up -d postgres redis
          </div>
          <div class="term-line">
            <span class="term-prompt">$</span> cp .env.example .env
          </div>
          <div class="term-line">
            <span class="term-prompt">$</span> python run.py start
          </div>
          <div class="term-line term-out">
            {{ $t('common.chiron_started_http_localhost_5173') }}
          </div>
        </div>
      </div>
    </section>

    <footer class="home-footer">
      <span class="home-footer-brand">
        <span class="home-footer-logo">MC</span>chiron
      </span>
      <span class="home-footer-note">{{ $t('common.self_hosted_open_source_your_data_stays_on_your_machine') }}</span>
    </footer>

    <!-- 滚动到顶部按钮（长页面辅助导航） -->
    <Transition name="top-fade">
      <button
        v-if="showTop"
        type="button"
        class="scroll-top"
        :title="$t('common.back_to_top')"
        :aria-label="$t('common.back_to_top')"
        @click="scrollToTop"
      >
        <ArrowUpOutlined />
      </button>
    </Transition>
  </div>
</template>

<style scoped>
.home { min-height: 100%; background: var(--bg-page); color: var(--text-primary); overflow-x: hidden; }

.hero {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: min(560px, 72vh);
  padding: 72px 24px 64px;
  text-align: center;
  overflow: hidden;
}
.hero-glow {
  position: absolute;
  inset: -30% -20% auto -20%;
  height: 85%;
  background:
    radial-gradient(ellipse 45% 55% at 50% 0%, var(--primary-bg), transparent 70%),
    radial-gradient(ellipse 30% 40% at 78% 12%, var(--accent-bg), transparent 70%),
    radial-gradient(ellipse 30% 40% at 22% 10%, var(--info-bg), transparent 70%);
  pointer-events: none;
  filter: blur(20px);
}
.hero-grid-bg {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(var(--border-subtle) 1px, transparent 1px),
    linear-gradient(90deg, var(--border-subtle) 1px, transparent 1px);
  background-size: 44px 44px;
  mask-image: radial-gradient(ellipse 60% 55% at 50% 0%, black 20%, transparent 75%);
  -webkit-mask-image: radial-gradient(ellipse 60% 55% at 50% 0%, black 20%, transparent 75%);
  pointer-events: none;
  opacity: 0.5;
}
.hero-content { position: relative; z-index: var(--z-local); max-width: 760px; }
.hero-badge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 14px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-full);
  background: var(--bg-surface);
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  box-shadow: var(--shadow-sm);
  animation: heroReveal var(--dur-slow) ease-out both;
}
.hero-badge-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--primary);
  animation: dotPulse var(--dur-pulse) ease-in-out infinite;
}
@keyframes dotPulse {
  0%, 100% { box-shadow: 0 0 4px var(--primary); }
  50% { box-shadow: 0 0 12px var(--primary); }
}
.hero-title {
  margin: 22px 0 16px;
  font-size: clamp(34px, 5.5vw, 56px);
  line-height: 1.15;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--text-primary);
  animation: heroReveal var(--dur-slow) 0.08s cubic-bezier(0.22, 0.8, 0.36, 1) both;
}
.hero-title-accent {
  background: linear-gradient(100deg, var(--primary), var(--accent));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  -webkit-text-fill-color: transparent;
}
.hero-sub {
  margin: 0 auto;
  font-size: 15px;
  line-height: 26px;
  color: var(--text-secondary);
  max-width: 560px;
  animation: heroReveal var(--dur-slow) 0.16s cubic-bezier(0.22, 0.8, 0.36, 1) both;
}
.hero-actions { display: flex; gap: 12px; justify-content: center; margin-top: 30px; animation: heroReveal var(--dur-slow) 0.24s cubic-bezier(0.22, 0.8, 0.36, 1) both; }
@keyframes heroReveal {
  from { opacity: 0; transform: translateY(16px); }
  to { opacity: 1; transform: translateY(0); }
}
.hero-cta {
  position: relative;
  overflow: hidden;
  border-radius: var(--radius-xl);
  padding: 0 26px;
  height: 44px;
  font-size: 14px;
  font-weight: 600;
  transition: transform var(--dur-normal) ease-out, box-shadow var(--dur-normal) ease, border-color var(--dur-normal) ease, background var(--dur-normal) ease;
}
.hero-cta.glow {
  box-shadow: var(--shadow-md), 0 4px 14px var(--primary-bg);
}
.hero-cta.glow:hover { box-shadow: var(--shadow-lg), 0 6px 20px var(--primary-bg); }
.hero-cta.ghost {
  border-color: var(--border-default);
  color: var(--text-primary);
  background: var(--bg-surface);
}
.hero-cta.ghost:hover { border-color: var(--primary); color: var(--primary); }

/* ── 特性网格 ── */
.features { max-width: 1080px; margin: 0 auto; padding: 40px 24px 56px; position: relative; }
.section-title { font-size: 28px; font-weight: 700; letter-spacing: -0.01em; text-align: center; }
.section-sub { margin-top: 8px; text-align: center; font-size: 14px; color: var(--text-tertiary); }
.feature-grid {
  position: relative;
  z-index: var(--z-content);
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
  margin-top: 34px;
}
.feature-card {
  position: relative;
  display: flex;
  flex-direction: column;
  padding: 22px 20px 18px;
  border: 1px solid var(--border-default);
  border-radius: var(--comp-card-radius);
  background: var(--bg-surface);
  box-shadow: var(--shadow-sm);
  cursor: pointer;
  transition: transform var(--dur-normal) var(--ease-out),
              border-color var(--dur-normal) var(--ease-out),
              box-shadow var(--dur-normal) var(--ease-out);
}
.feature-card.visible {
  animation: cardIn var(--dur-slow) cubic-bezier(0.22, 0.8, 0.36, 1) both;
  animation-delay: var(--card-delay, 0ms);
}
@keyframes cardIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: none; }
}
.feature-card:hover {
  transform: translateY(-2px);
  border-color: var(--primary);
  box-shadow: var(--shadow-md), 0 0 0 3px var(--primary-bg);
}
.feature-card:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.feature-icon-wrap {
  width: 42px; height: 42px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 10px;
  background: var(--primary-bg);
  color: var(--primary);
  margin-bottom: 14px;
}
.feature-icon { font-size: 20px; }
.feature-en {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.16em;
  color: var(--text-tertiary);
  text-transform: uppercase;
  margin-bottom: 4px;
}
.feature-title { font-size: 15px; font-weight: 600; color: var(--text-primary); }
.feature-desc { margin-top: 6px; font-size: 13px; line-height: 20px; color: var(--text-secondary); flex: 1; }
.feature-go { margin-top: 12px; font-size: 12px; color: var(--primary); opacity: 0; transform: translateX(-4px); transition: opacity var(--dur-normal) ease, transform var(--dur-normal) ease; }
.feature-card:hover .feature-go { opacity: 1; transform: translateX(0); }

/* ── 产品展示：工作台窗口预览 ── */
.showcase { max-width: 1080px; margin: 0 auto; padding: 24px 24px 56px; position: relative; }
.showcase-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 18px;
  margin-top: 34px;
}
.window-card {
  border-radius: 14px;
  overflow: hidden;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  box-shadow: var(--shadow-md);
  transition: transform var(--dur-normal) ease, box-shadow var(--dur-normal) ease;
}
.window-card:hover { transform: translateY(-3px); box-shadow: var(--shadow-lg); }
.window-chrome {
  display: flex;
  align-items: center;
  gap: 7px;
  height: 36px;
  padding: 0 12px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-surface-hover);
}
.win-dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
.win-dot.red { background: var(--traffic-red); }
.win-dot.yellow { background: var(--traffic-amber); }
.win-dot.green { background: var(--traffic-green); }
.win-title { flex: 1; text-align: center; font-size: 11px; color: var(--text-tertiary); margin-right: 30px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.window-body { padding: 16px; }

.chat-preview { display: flex; flex-direction: column; gap: 10px; min-height: 200px; }
.pv-msg { display: flex; }
.pv-msg.user { justify-content: flex-end; }
.pv-bubble {
  max-width: 82%;
  padding: 8px 12px;
  border-radius: 14px;
  border-bottom-left-radius: 4px;
  background: var(--bg-code);
  border: 1px solid var(--border-subtle);
  font-size: 12px;
  line-height: 19px;
  color: var(--text-primary);
}
.pv-msg.user .pv-bubble {
  border-radius: 14px;
  border-bottom-right-radius: 4px;
  background: var(--primary-bg);
  border-color: var(--primary-border);
}
.pv-tool {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--bg-surface-hover);
  border: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
}
.pv-tool-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--primary);
}
.pv-input {
  margin-top: 2px;
  padding: 8px 12px;
  border-radius: 12px;
  border: 1px solid var(--border-default);
  background: var(--bg-surface-hover);
  font-size: 12px;
  color: var(--text-tertiary);
}

.panel-preview { display: flex; flex-direction: column; gap: 6px; min-height: 200px; }
.pv-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 8px 8px;
  border-bottom: 1px solid var(--border-subtle);
  margin-bottom: 4px;
}
.pv-panel-title { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.pv-panel-caret { font-size: 10px; color: var(--text-tertiary); }
.pv-timeline {
  height: 34px;
  border-radius: 8px;
  background: var(--bg-surface-hover);
  border: 1px solid var(--border-subtle);
  position: relative;
}
.pv-timeline-track { position: absolute; inset: 12px 10px; }
.pv-span {
  position: absolute;
  top: 0;
  height: 8px;
  border-radius: 2px;
  background: linear-gradient(90deg, var(--primary), var(--accent));
  opacity: 0.85;
}
.pv-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.pv-row.active { background: var(--primary-bg); color: var(--primary); font-weight: 600; }
.pv-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--primary); opacity: 0.7; }

/* ── 快速开始：终端代码块 ── */
.quickstart { max-width: 760px; margin: 0 auto; padding: 24px 24px 64px; }
.terminal-card {
  margin-top: 30px;
  border-radius: 14px;
  overflow: hidden;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  box-shadow: var(--shadow-md);
}
.terminal-card .window-chrome { background: var(--bg-surface-hover); }
.term-copy {
  flex: none;
  margin-left: auto;
  min-height: 28px;
  border: 1px solid var(--border-default);
  border-radius: 6px;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 11px;
  padding: 4px 12px;
  cursor: pointer;
  transition: color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.term-copy:hover { color: var(--primary); border-color: var(--primary); }
.terminal-body { padding: 16px 18px; font-family: var(--font-mono); font-size: 12.5px; line-height: 24px; background: var(--bg-code); }
.term-line { color: var(--text-primary); white-space: pre-wrap; word-break: break-all; }
.term-prompt { color: var(--primary); font-weight: 600; margin-right: 8px; }
.term-out { color: var(--text-tertiary); margin-top: 6px; }

/* ── 页脚 ── */
.home-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 18px 28px;
  border-top: 1px solid var(--border-subtle);
  font-size: 12px;
  color: var(--text-tertiary);
  background: var(--bg-surface);
}
.home-footer-brand { display: flex; align-items: center; gap: 8px; font-weight: 600; color: var(--text-secondary); }
.home-footer-logo {
  width: 20px; height: 20px; border-radius: 6px;
  background: linear-gradient(135deg, var(--primary), var(--accent));
  color: var(--on-solid); font-size: 9px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center;
}

@media (max-width: 640px) {
  .hero { min-height: 520px; padding: 56px 20px 40px; }
  .hero-br { display: none; }
  .hero-actions { flex-direction: column; align-items: center; }
  .feature-grid { grid-template-columns: 1fr; }
  .showcase-grid { grid-template-columns: 1fr; }
  .home-footer { flex-direction: column; text-align: center; }
}
@media (max-width: 768px) and (min-width: 641px) {
  .feature-grid { grid-template-columns: repeat(2, 1fr); }
  .showcase-grid { grid-template-columns: 1fr; }
  .hero-title { font-size: clamp(30px, 6vw, 44px); }
}
@media (max-width: 640px) {
  .features, .showcase { padding-left: 16px; padding-right: 16px; }
  .quickstart { padding-left: 16px; padding-right: 16px; }
  .feature-grid { gap: 12px; }
  .hero-cta { width: 100%; max-width: 320px; }
}
@media (hover: none) {
  .feature-card:hover { transform: none; }
  .hero-cta:hover { transform: none; }
}
@media (prefers-reduced-motion: reduce) {
  .hero-badge, .hero-title, .hero-sub, .hero-actions,
  .feature-card.visible, .hero-badge-dot { animation: none; }
  .feature-card { opacity: 1; }
  .hero-cta { transition: none; }
  .scroll-top { transition: none; }
}

.scroll-top {
  position: fixed;
  right: 24px;
  bottom: 24px;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-primary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  z-index: var(--z-sticky);
  box-shadow: var(--shadow-md);
  transition: transform var(--dur-normal) ease, border-color var(--dur-normal) ease, background var(--dur-normal) ease;
}
.scroll-top:hover {
  transform: translateY(-2px);
  border-color: var(--primary);
  color: var(--primary);
}
.scroll-top:active { transform: translateY(0) scale(0.94); }
.top-fade-enter-active, .top-fade-leave-active { transition: opacity var(--dur-normal) ease, transform var(--dur-normal) ease; }
.top-fade-enter-from, .top-fade-leave-to { opacity: 0; transform: translateY(8px); }
</style>