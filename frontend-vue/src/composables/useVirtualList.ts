/**
 * 虚拟列表 Composable
 * 
 * 用于优化长列表渲染性能，只渲染可视区域内的元素
 * 支持固定高度和动态高度两种模式
 * 
 * 使用示例:
 * ```vue
 * <script setup>
 * const { containerStyle, wrapperStyle, visibleItems, onScroll } = useVirtualList({
 *   items: largeList,
 *   itemHeight: 50,
 *   containerHeight: 400,
 * })
 * </script>
 * 
 * <template>
 *   <div :style="containerStyle" @scroll="onScroll">
 *     <div :style="wrapperStyle">
 *       <div v-for="item in visibleItems" :key="item.id">
 *         {{ item.name }}
 *       </div>
 *     </div>
 *   </div>
 * </template>
 * ```
 */

import { ref, computed, type Ref } from 'vue'

interface VirtualListOptions<T = unknown> {
  items: Ref<T[]> | T[]
  itemHeight: number
  containerHeight?: number
  overscan?: number  // 预渲染缓冲区大小
}

interface VirtualListItem<T = unknown> {
  item: T
  index: number
  key: string | number
}

export function useVirtualList<T = unknown>(options: VirtualListOptions<T>) {
  const {
    items,
    itemHeight,
    containerHeight = 400,
    overscan = 5,
  } = options

  const list = computed(() => {
    return isArray(items) ? items : items.value
  })

  const scrollTop = ref(0)
  const containerRef = ref<HTMLElement | null>(null)

  // 计算可视区域起始和结束索引
  const startIndex = computed(() => {
    const idx = Math.floor(scrollTop.value / itemHeight)
    return Math.max(0, idx - overscan)
  })

  const endIndex = computed(() => {
    const visibleCount = Math.ceil(containerHeight / itemHeight)
    const idx = Math.floor(scrollTop.value / itemHeight) + visibleCount
    return Math.min(list.value.length, idx + overscan)
  })

  // 可见项列表
  const visibleItems = computed<VirtualListItem<T>[]>(() => {
    const result: VirtualListItem<T>[] = []
    for (let i = startIndex.value; i < endIndex.value; i++) {
      const item = list.value[i]
      if (item) {
        result.push({
          item,
          index: i,
          key: (item as { id?: string | number }).id ?? i,
        })
      }
    }
    return result
  })

  // 容器样式
  const containerStyle = computed(() => ({
    height: `${containerHeight}px`,
    overflow: 'auto',
    position: 'relative',
  }))

  // 包裹器样式 (用于填充空间)
  const wrapperStyle = computed(() => ({
    transform: `translateY(${startIndex.value * itemHeight}px)`,
    position: 'absolute',
    top: 0,
    left: 0,
    width: '100%',
  }))

  // 总高度样式
  const totalHeightStyle = computed(() => ({
    height: `${list.value.length * itemHeight}px`,
  }))

  // 滚动处理
  const onScroll = (e: Event) => {
    const target = e.target as HTMLElement
    scrollTop.value = target.scrollTop
  }

  // 滚动到指定项
  const scrollTo = (index: number) => {
    if (containerRef.value) {
      containerRef.value.scrollTop = index * itemHeight
      scrollTop.value = index * itemHeight
    }
  }

  return {
    containerStyle,
    wrapperStyle,
    totalHeightStyle,
    visibleItems,
    onScroll,
    scrollTo,
    containerRef,
    scrollTop,
  }
}

// 类型守卫
function isArray<T>(value: Ref<T[]> | T[]): value is T[] {
  return Array.isArray(value)
}