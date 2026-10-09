<script setup lang="ts">
/**
 * 未配置时的引导浮层（antd `Tour` 的薄封装）。
 *
 * ## 对外只有一个入口
 *
 * `offer(reason, hasModel)` —— 由 `ChatView` 在**被拦下的那一刻**调用：
 *  - `'no-model'`：没有可用模型（发送被 `ensureModel` 拦下）
 *  - `'provider-auth'`：服务商认证失败（SSE `error` 帧带 `AuthenticationError` / 401）
 *
 * 返回 `true` 表示**真的弹了** ⇒ 调用方此时不要再叠一句 toast（双重打扰）。
 *
 * ## 语义分工（照 antd `Tour` 的事件）
 *
 * - **关闭（右上角 ✗）** ⇒ 只是收起，**不跳转**（用户可能只是不想看）✓
 * - **走到最后一步点主按钮（= antd 的"完成"）** ⇒ 收起 **并且**（管理员）跳去 `/models` ✓
 *   非管理员**不跳转** —— `/models` 是 `requiresAdmin`（`router/index.ts:46`），
 *   点了也进不去，所以那一步的文案直接是"请联系管理员" ✓
 *
 * ## 决策不在这里
 *
 * 该不该弹 / 弹几步 / 文案，全在 `composables/useOnboardingTour.ts` ⇒ 那部分能单测，
 * 这个组件只做"接到 antd 上"（本仓没有浏览器 e2e，所以**能测的必须都在接缝里**）。
 */
import { computed } from 'vue'
import { Tour } from 'ant-design-vue'
import type { TourProps } from 'ant-design-vue'
import { useOnboardingTour, type TourReason } from '../../composables/useOnboardingTour'
import { useAuthStore } from '../../stores/auth'
const emit = defineEmits<{ (e: 'go-configure'): void }>()

const auth = useAuthStore()
const tour = useOnboardingTour({
  userId: () => auth.user?.id,
  isAdmin: () => auth.isAdmin,
})
const { open, current, steps, animated, finish } = tour

/**
 * 最后一步的主按钮 = antd 的"完成"按钮 ⇒ 走 `finish` 事件。
 * 管理员在那一步的文案是"去配置"，所以完成时顺手跳转；其余情况只收起。
 */
function onFinish(): void {
  finish()
  if (auth.isAdmin) emit('go-configure')
}

function onClose(): void {
  finish()
}

/**
 * antd 的 `target` 要的是 `(() => HTMLElement) | (() => null)`（函数的**联合**），
 * 而我们的签名是 `() => HTMLElement | null`（返回值的联合）—— 语义等价但类型不兼容，
 * 所以在这一处边界上转一次，别把 antd 的怪类型传染进决策层。
 */
const renderedSteps = computed(() => steps.value as unknown as TourProps['steps'])

function offer(reason: TourReason, hasModel: boolean): boolean {
  return tour.offer(reason, hasModel)
}

defineExpose({ offer })
</script>

<template>
  <Tour
    :open="open"
    :current="current"
    :steps="renderedSteps"
    :animated="animated"
    :mask="true"
    @change="current = $event"
    @close="onClose"
    @finish="onFinish"
  />
</template>
