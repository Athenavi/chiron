<script setup lang="ts">
/**
 * 媒体页的右键菜单（渲染层）。
 *
 * **只做渲染与派发**，不判断任何条件 —— 可见性、可用性、不可用原因全部来自
 * `menuModel.buildMenuModel()`（纯函数，已有单测）。这样做的直接好处：
 * 菜单的规则可以在测试里穷举，而不用挂载组件、模拟右键、再断言某个按钮灰了。
 *
 * 三个刻意的取舍：
 *
 * 1. **用 antd 的 `trigger="contextmenu"`**，不自研定位。右键菜单的边缘翻转、
 *    ESC 关闭、点击外部关闭、焦点回退，antd 都已经做对；自己写一遍就是把
 *    这些易错且无业务价值的细节重新踩一遍。定位由库负责，本组件不碰坐标。
 * 2. **禁用项带 `title` 显示原因**。只灰不解释等于让用户猜 —— 而这里灰色的原因
 *    有五种（没选、选多了、剪贴板空、后端不支持、类型不一致），不写出来就不可区分。
 * 3. **分组用 divider 而不是靠间距**。Windows 的资源管理器也是分组的
 *    （打开 / 剪贴板 / 改名删除 / 查看），位置稳定才能形成肌肉记忆。
 */
import { computed, h } from 'vue'
import { Dropdown, Menu, type MenuProps } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'

import { buildMenuDescriptors, type MenuAction, type MenuModel } from './menuModel'

const props = defineProps<{
  /** 由 `buildMenuModel` 求值得到的完整菜单模型 */
  model: MenuModel
  /** 是否禁用整个菜单（例如正在批量操作中） */
  disabled?: boolean
}>()

const emit = defineEmits<{
  (e: 'select', action: MenuAction): void
}>()

const { t } = useI18n()

/** 构造 antd Menu 的 items（含分隔线与禁用原因）。 */
const menuItems = computed<MenuProps['items']>(() => {
  // 类型直接取 antd 的 `ItemType`：手写 `Record<string, unknown>` 会在 `:items` 处
  // 报"不可赋值"，因为 Menu 要求的是判别联合而不是任意对象。
  type Item = NonNullable<MenuProps['items']>[number]

  // 项的挑选、分组、剔除逻辑全在 `buildMenuDescriptors`（纯函数，有单测）。
  // 这里只做"描述 → antd 结构"的机械映射 —— 浮层是懒渲染的，逻辑留在这里就没法测。
  const items: Item[] = []
  for (const d of buildMenuDescriptors(props.model)) {
    if (d.dividerBefore) items.push({ type: 'divider' } as Item)
    const label = t(d.labelKey)
    items.push({
      key: d.action,
      // 这里必须传**求值后的 VNode**，不能传 `() => h(...)`。
      //
      // antd 内部用 `{{ label }}` 插值渲染 label，Vue 对函数会走 `String()`，
      // 于是菜单上直接显示出 `() => h("span", ...)` 的函数源码 —— 曾经真的出现过。
      // 函数作为 label 只在"渲染函数插槽"语义下成立，而 Menu 的 items 是数据描述，
      // 不提供这种语义。
      label: d.shortcut
        ? h('span', { class: 'ctx-label-row' }, [
            h('span', { class: 'ctx-label' }, label),
            h('span', { class: 'ctx-shortcut' }, d.shortcut),
          ])
        : label,
      disabled: d.disabled,
      // 禁用时把原因挂到 title 上：只灰不解释等于让用户猜
      title: d.disabled && d.reasonKey ? t(d.reasonKey) : undefined,
    } as Item)
  }
  return items
})

function onMenuClick(info: { key: string | number }): void {
  emit('select', String(info.key) as MenuAction)
}
</script>

<template>
  <Dropdown
    :trigger="['contextmenu']"
    :disabled="props.disabled"
    placement="bottomLeft"
  >
    <!--
      触发区域由调用方决定（网格容器 / 单个卡片 / 列表行）。
      这里用 `display: contents` 的插槽：既把右键事件绑到子元素上，
      又不会引入一个额外的布局盒子把网格布局撑坏。
    -->
    <div class="ctx-trigger">
      <slot />
    </div>
    <template #overlay>
      <Menu
        class="ctx-menu"
        :items="menuItems"
        @click="onMenuClick"
      />
    </template>
  </Dropdown>
</template>

<style scoped>
.ctx-trigger { display: contents; }

/* 菜单本体：宽度与行高按资源管理器的密度，但不写死像素字号（走 token） */
.ctx-menu { min-width: 200px; }

.ctx-label-row {
  display: flex;
  align-items: center;
  gap: 24px;
  justify-content: space-between;
}

.ctx-label { flex: 1; }

/* 快捷键提示弱化：它是辅助信息，不该和动作名抢注意力 */
.ctx-shortcut {
  color: var(--text-tertiary);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}
</style>
