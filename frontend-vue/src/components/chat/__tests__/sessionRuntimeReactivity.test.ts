import { describe, expect, it } from 'vitest'
import { computed, nextTick, reactive, ref, shallowRef, watchEffect } from 'vue'

/**
 * 回归测试：ChatView 的多会话运行时状态**必须是响应式的**。
 *
 * 事故背景：`items` / `loading` 曾经从 `ref` 改成"从 Map 里取当前会话的切片"的 computed，
 * 而底层 `runtimes` 用了**普通 Map**、`writingRun` 用了**普通 let 变量** ——
 * 两者都不是响应式的，于是：
 *   1. `items.value.push(...)` 不触发任何更新 → 流式期间界面不动，
 *      直到 `loading` 在结束时变化才一次性刷新（"消息突然全部跳出"）；
 *   2. `computed` 的缓存不会因 `writingRun` 失效 → SSE 回写会落到**当前会话**而不是
 *      事件所属会话（跨会话串态）。
 * 这两点**逻辑测试测不出来**，所以这里专门针对"响应性"本身写断言。
 *
 * 下面是与 ChatView.vue 中 `runtimes` / `runOf` / `currentRun` / `items` / `writingRun`
 * 同构的最小复现；形态一旦退回"普通 Map / 普通变量"，这些用例即失败。
 */
interface RunState {
  items: string[]
  loading: boolean
  gen: number
}

function makeStore() {
  const runtimes = reactive(new Map<string, RunState>())
  const activeId = ref('a')
  const writingRun = shallowRef<RunState | null>(null)

  function runOf(sid: string): RunState {
    let run = runtimes.get(sid)
    if (!run) {
      runtimes.set(sid, { items: [], loading: false, gen: 1 })
      // 关键：取回代理而不是刚构造的原始对象
      run = runtimes.get(sid)!
    }
    return run
  }

  const currentRun = computed(() => runOf(activeId.value || '__none__'))

  const items = computed<string[]>({
    get: () => (writingRun.value ?? currentRun.value).items,
    set: (v) => {
      ;(writingRun.value ?? currentRun.value).items = v
    },
  })

  const loading = computed<boolean>({
    get: () => (writingRun.value ?? currentRun.value).loading,
    set: (v) => {
      ;(writingRun.value ?? currentRun.value).loading = v
    },
  })

  function withRun<T>(run: RunState, fn: () => T): T {
    const prev = writingRun.value
    writingRun.value = run
    try {
      return fn()
    } finally {
      writingRun.value = prev
    }
  }

  return { runtimes, activeId, runOf, items, loading, withRun }
}

describe('会话运行时状态的响应性', () => {
  it('items.value.push(...) 必须触发依赖更新（否则流式界面不刷新）', async () => {
    const { items } = makeStore()
    const seen: number[] = []
    watchEffect(() => {
      seen.push(items.value.length)
    })

    items.value.push('m1')
    await nextTick()

    // 退化成普通 Map/数组时这里只会看到 [0]
    expect(seen).toContain(1)
  })

  it('items.value = [...] 整体替换必须触发更新', async () => {
    const { items } = makeStore()
    const seen: number[] = []
    watchEffect(() => {
      seen.push(items.value.length)
    })

    items.value = ['a', 'b']
    await nextTick()

    expect(seen).toContain(2)
  })

  it('withRun 必须是响应式的：否则 computed 缓存会让回写落到当前会话', () => {
    const { runOf, items, withRun, activeId } = makeStore()
    activeId.value = 'a' // 当前视图是 a

    // 以 b 为作用域回写（模拟 SSE 事件属于会话 b）
    withRun(runOf('b'), () => items.value.push('for-b'))

    expect(runOf('b').items).toEqual(['for-b'])
    // a 绝不能被污染 —— writingRun 若退化成普通变量，这里会变成 ['for-b']
    expect(runOf('a').items).toEqual([])
  })

  it('按会话隔离：各自追加互不影响，切换视图读到各自的列表', () => {
    const { runOf, items, withRun, activeId } = makeStore()

    withRun(runOf('a'), () => items.value.push('a1'))
    withRun(runOf('b'), () => items.value.push('b1'))
    withRun(runOf('a'), () => items.value.push('a2'))

    activeId.value = 'a'
    expect(items.value).toEqual(['a1', 'a2'])
    activeId.value = 'b'
    expect(items.value).toEqual(['b1'])
  })

  it('loading 按会话独立：一个会话在跑不影响另一个会话的视图', async () => {
    const { runOf, loading, withRun, activeId } = makeStore()

    activeId.value = 'a'
    expect(loading.value).toBe(false)

    // 会话 b 开始生成
    withRun(runOf('b'), () => {
      loading.value = true
    })
    await nextTick()

    // 当前视图是 a，不应被 b 的运行状态影响
    expect(loading.value).toBe(false)

    activeId.value = 'b'
    expect(loading.value).toBe(true)
  })

  it('对照组：普通 Map + 普通数组确实不触发更新（证明上面的断言有区分能力）', async () => {
    // 刻意复现事故里的形态：普通 Map、非响应式对象
    const plain = new Map<string, RunState>()
    plain.set('a', { items: [], loading: false, gen: 1 })

    const derived = computed(() => plain.get('a')!.items)
    const seen: number[] = []
    watchEffect(() => {
      seen.push(derived.value.length)
    })

    derived.value.push('x')
    await nextTick()

    // 只有初始的 0 —— push 从未被观察到。这正是"界面不刷新"的机理；
    // 若上面的用例退化成这个形态，它们同样会失败。
    expect(seen).toEqual([0])
  })
})
