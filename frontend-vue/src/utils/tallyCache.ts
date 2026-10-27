/**
 * 「打开界面就触发的昂贵只读统计」的四条规则。
 *
 * 缘起：上游 `vendor/DeepSeek-Reasonix` 提交 `f41b9d413`
 * （`fix(desktop): opening settings no longer walks the disk for the storage total`，
 * 落地在 `desktop/frontend-next/src/ui/storageTally.ts` + `ui/Settings.tsx`）的结论 ——
 * 这类操作的毛病不是「算得慢，需要一个更快的算法」，而是
 * **用户只是路过设置页，却替一个他还没看的数字付了全表扫的钱**。
 * 所以在调用侧让同一份请求同时具备四件事：
 *
 *   ① **延后** 面板打开后隔 N 毫秒再发 —— 路过不算；
 *   ② **TTL 缓存**  N 分钟内再要同一个数字，直接把上次的答案给他，不发请求；
 *   ③ **并发合并**  同一 key 同时来多个调用者，共享同一个 Promise，只发一次；
 *   ④ **失败不缓存** 失败必须清掉 in-flight 记录、不写缓存，下次仍能重试
 *      （把失败记成结果，等于这个数字永远错到底）。
 *
 * 另加 `invalidate()`：数据**真的**变了（删除会话 / 撤销分享 / 解绑）时由调用方显式作废 ——
 * 「刚删完会话，计数还停在旧值」比慢更要命。
 *
 * 这里不替调用方决定「什么该缓存」：它只是原语，由调用方对每个请求逐个判断成本后接入。
 */

/**
 * TTL 默认 10 分钟。
 *
 * 这批数字（会话数、分享数、绑定数）只在**用户自己动手**时变，而所有会改变它们的操作
 * （删除会话、撤销分享、解绑三方账号）都在同一个面板里，那些路径一律 `force` 重取。
 * 10 分钟足够覆盖「反复开关设置」这种最常见的来回，又不至于让别处发生的变化长期不可见。
 */
export const TALLY_TTL_MS = 10 * 60 * 1000

/**
 * 延后默认 1500 毫秒（与上游 `STORAGE_TALLY_DELAY` 同值）。
 *
 * 取值的理由是**首屏在哪**：设置面板打开落的是「通用」页签（主题 / 朗读 / 语言），
 * 这批统计全在后面的「账户」「数据管理」页签里。1.5s 足够滤掉「点一下看看又关掉」的往返，
 * 又短到用户真去点那几个页签时请求基本已经发出（或已命中缓存）——不影响任何人看到的数字。
 */
export const TALLY_DELAY_MS = 1500

type Settled<T> = { at: number; value: T }
type InFlight<T> = { flight: Promise<T> }
type Entry<T> = Settled<T> | InFlight<T>

export interface TallyReadOptions {
  /** 绕过已缓存的值，重新取一次（手动刷新 / 数据变更后用它，别读旧值） */
  force?: boolean
  /** TTL 覆盖，默认 {@link TALLY_TTL_MS} */
  ttl?: number
  /** 时钟注入，便于测试；默认 `Date.now` */
  now?: () => number
}

export interface TallyPeekOptions {
  ttl?: number
  now?: () => number
}

export interface TallyCache {
  /**
   * 取一个 key 的值：命中 TTL ⇒ 立刻给缓存；有在飞请求 ⇒ 复用它；否则发一次新的。
   * 失败时抛给调用方，且**不留任何记录**。
   *
   * 约定：被缓存的返回值不能是 `undefined`（否则无法与「没有缓存」区分）。数组/对象都可以。
   */
  load: <T>(key: string, fetcher: () => Promise<T>, options?: TallyReadOptions) => Promise<T>
  /** 同步看一眼已有的值（TTL 内才有）；没有或不新鲜返回 `undefined`，不发请求 */
  peek: <T>(key: string, options?: TallyPeekOptions) => T | undefined
  /** 该 key 是否正有一个请求在飞 */
  inFlight: (key: string) => boolean
  /** 作废一个 key；不传 key 就清空全部（账号切换这类全局事件用） */
  invalidate: (key?: string) => void
}

export function createTallyCache(defaultTtl: number = TALLY_TTL_MS): TallyCache {
  const entries = new Map<string, Entry<unknown>>()

  async function load<T>(
    key: string,
    fetcher: () => Promise<T>,
    options: TallyReadOptions = {},
  ): Promise<T> {
    const now = options.now ?? Date.now
    const ttl = options.ttl ?? defaultTtl

    // force = 「我要现在的」⇒ 先把这条记录整个扔掉（含 in-flight），再按正常路径走，
    // 这样它不会命中任何旧东西，也不会被任何旧东西回写覆盖。
    if (options.force) entries.delete(key)

    const held = entries.get(key) as Entry<T> | undefined
    // ③ 并发合并：同一 key 已经有请求在飞，直接共享它，不重复发
    if (held && 'flight' in held) return held.flight
    // ② TTL 命中：还新鲜就不要再问第二次
    if (held && now() - held.at < ttl) return held.value

    // 用 async 包装：即便 fetcher 同步抛，也得到一个 rejected promise 而不是同步异常
    const flight: Promise<T> = (async () => fetcher())()
    const entry: InFlight<T> = { flight }
    entries.set(key, entry)

    try {
      const value = await flight
      // 只在记录仍是自己那条时才写回 —— 否则（force 抢先）会把更新的在飞请求盖掉
      if (entries.get(key) === entry) entries.set(key, { at: now(), value })
      return value
    } catch (error) {
      // ④ 失败不缓存：清掉 in-flight，让下一次调用真的重试
      if (entries.get(key) === entry) entries.delete(key)
      throw error
    }
  }

  function peek<T>(key: string, options: TallyPeekOptions = {}): T | undefined {
    const held = entries.get(key) as Entry<T> | undefined
    if (!held || 'flight' in held) return undefined
    const now = options.now ?? Date.now
    return now() - held.at < (options.ttl ?? defaultTtl) ? held.value : undefined
  }

  function inFlight(key: string): boolean {
    const held = entries.get(key)
    return held !== undefined && 'flight' in held
  }

  function invalidate(key?: string): void {
    if (key === undefined) entries.clear()
    else entries.delete(key)
  }

  return { load, peek, inFlight, invalidate }
}

/**
 * 进程内共享的缓存（设置面板的统计数据都放这里）。
 * 跨组件挂载存活是**故意的**：TTL 缓存的意义就是「把面板关掉再打开不必重新扫一遍」。
 */
export const tallyCache: TallyCache = createTallyCache()

export interface TallyScheduleOptions {
  /** 延迟毫秒，默认 {@link TALLY_DELAY_MS} */
  delayMs?: number
  /**
   * 缓存未命中、即将进入「等 delay 后发请求」时调用一次 —— 调用方据此从此刻起
   * 显示为加载中（命中缓存时**不**调用，否则会白闪一下转圈）。
   */
  onStart?: () => void
  /** 失败回调。注意延后 + 失败不会自动重试：下次触发（重新打开面板 / 手动刷新）才算重试 */
  onError?: (error: unknown) => void
  /** 时钟注入，只用于判断缓存是否新鲜；默认 `Date.now` */
  now?: () => number
}

/**
 * 面板打开时用它代替直接发请求：**延后 + 缓存命中即渲染**。
 *
 * - 已有新鲜缓存 ⇒ **立即**给值、不发请求（延后是为了省掉「路过」的那一次，
 *   不是为了把已经拿到的数字也藏 1.5 秒）；
 * - 否则延后 `delayMs` 才真正请求，请求本身仍走 {@link TallyCache.load}
 *   （于是并发合并、失败不缓存这两条继续生效）。
 *
 * 返回的函数用于取消（组件卸载时必须调用）：否则一个已经被关掉的弹窗还会在 1.5 秒后
 * 发请求、还会往已经不看的组件里写状态。
 */
export function scheduleTally<T>(
  cache: TallyCache,
  key: string,
  fetcher: () => Promise<T>,
  onValue: (value: T) => void,
  options: TallyScheduleOptions = {},
): () => void {
  const known = cache.peek<T>(key, { now: options.now })
  if (known !== undefined) {
    onValue(known)
    return () => {}
  }
  // 命中缓存的分支已经在上面 return 了 ⇒ 能走到这里就一定会有一次真实请求（或已有一个在飞）
  options.onStart?.()

  const handle = setTimeout(() => {
    // 两个 handler 分开给：onValue 里若抛错，不该被当成「请求失败」吞给 onError
    cache.load(key, fetcher, { now: options.now }).then(
      (value) => { onValue(value) },
      (error) => { options.onError?.(error) },
    )
  }, options.delayMs ?? TALLY_DELAY_MS)

  return () => { clearTimeout(handle) }
}
