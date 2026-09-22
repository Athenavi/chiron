import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  GRID,
  MAX_SCALE,
  MIN_SCALE,
  clampScale,
  nextFreeSlot,
  occupiedSlots,
  snap,
  useSessionMap,
} from '../useSessionMap'

/** 极简 localStorage stub（隐私模式/损坏数据也要能降级，所以不走真实实现） */
function installStorageStub() {
  const store = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size
    },
  })
  return store
}

describe('会话地图 —— 独立记忆层', () => {
  beforeEach(() => {
    installStorageStub()
  })

  // ── 这条是整个功能的立身之本 ──
  it('★ 铁律：导入新节点不得改变任何既有节点的坐标', () => {
    const map = useSessionMap()
    map.importSessions([{ sessionId: 'a' }, { sessionId: 'b' }, { sessionId: 'c' }])
    const before = JSON.parse(JSON.stringify(map.nodes.value)) as typeof map.nodes.value

    // 再导入两个
    map.importSessions([{ sessionId: 'd' }, { sessionId: 'e' }])

    // 既有三个的位置必须**逐字节相同**（空间记忆失效的根因就是这里被动过）
    const after = map.nodes.value
    for (const prev of before) {
      const now = after.find(n => n.id === prev.id)
      expect(now).toMatchObject({ x: prev.x, y: prev.y })
    }
    expect(after.length).toBe(5)
  })

  it('节点是独立实体：id 不是 session_id，且带自己的名字', () => {
    const map = useSessionMap()
    const { created } = map.importSessions([{ sessionId: 'a', title: '会话原标题' }])
    const node = map.nodes.value[0]
    expect(node.id).not.toBe('a') // 有自己的 id
    expect(node.id).toBe(created[0])
    expect(node.sessionId).toBe('a')

    map.renameNode(node.id, '地图上的新名字')
    expect(map.nodes.value[0].title).toBe('地图上的新名字')
  })

  it('importSessions 默认跳过已在地图中的会话', () => {
    const map = useSessionMap()
    map.importSessions([{ sessionId: 'a' }])
    const r = map.importSessions([{ sessionId: 'a' }, { sessionId: 'b' }])
    expect(r.skipped).toEqual(['a'])
    expect(r.created.length).toBe(1)
    expect(map.nodes.value.length).toBe(2)
  })

  it('同一会话可被引用多次（地图层独立于会话列表）', () => {
    const map = useSessionMap()
    map.importSessions([{ sessionId: 'a' }, { sessionId: 'a' }], { skipExisting: false })
    expect(map.nodes.value.length).toBe(2)
    expect(map.nodes.value.every(n => n.sessionId === 'a')).toBe(true)
    // 两个节点 id 不同、位置不同
    expect(map.nodes.value[0].id).not.toBe(map.nodes.value[1].id)
    expect(map.nodes.value[0].x).not.toBe(map.nodes.value[1].x)
  })

  it('支持纯便签节点（无 sessionId）', () => {
    const map = useSessionMap()
    const id = map.addNote(391, -117, '记得问一下限流阈值')
    const n = map.nodes.value.find(v => v.id === id)!
    expect(n.sessionId).toBeUndefined()
    expect(n.title).toBe('记得问一下限流阈值')
    expect(n.x % GRID).toBe(0) // 便签也吸附
  })

  it('removeNode 只删地图节点，不影响任何其它节点', () => {
    const map = useSessionMap()
    map.importSessions([{ sessionId: 'a' }, { sessionId: 'b' }])
    const victim = map.nodes.value[0]
    const survivor = JSON.parse(JSON.stringify(map.nodes.value[1]))

    map.removeNode(victim.id)

    expect(map.nodes.value.length).toBe(1)
    expect(map.nodes.value[0]).toMatchObject({ id: survivor.id, x: survivor.x, y: survivor.y })
  })

  it('隐藏 ≠ 删除：hidden 可逆且节点仍在', () => {
    const map = useSessionMap()
    map.importSessions([{ sessionId: 'a' }])
    const id = map.nodes.value[0].id

    map.setHidden(id, true)
    expect(map.nodes.value[0].hidden).toBe(true)
    expect(map.nodes.value.length).toBe(1) // 仍在，只是不显示

    map.setHidden(id, false)
    expect(map.nodes.value[0].hidden).toBe(false)
  })

  it('成组 / 解组：只改地图层的归属', () => {
    const map = useSessionMap()
    map.importSessions([{ sessionId: 'a' }, { sessionId: 'b' }, { sessionId: 'c' }])
    const ids = map.nodes.value.map(n => n.id)
    const gid = map.groupNodes([ids[0], ids[1]], '支付重构')

    expect(map.groups.value.find(g => g.id === gid)?.name).toBe('支付重构')
    expect(map.nodes.value[0].groupId).toBe(gid)
    expect(map.nodes.value[1].groupId).toBe(gid)
    expect(map.nodes.value[2].groupId).toBeUndefined()

    map.ungroup(gid)
    expect(map.nodes.value[0].groupId).toBeUndefined()
    expect(map.groups.value.length).toBe(0)
  })

  it('拖动吸附到网格并标记 pinned', () => {
    const map = useSessionMap()
    map.addNote(0, 0)
    const id = map.nodes.value[0].id
    map.moveNode(id, 391, -117)
    expect(map.nodes.value[0]).toMatchObject({ x: snap(391), y: snap(-117), pinned: true })
  })

  it('落位互不重叠；nextFreeSlot 跳过已占用的格子', () => {
    const map = useSessionMap()
    map.importSessions([{ sessionId: 'a' }, { sessionId: 'b' }, { sessionId: 'c' }, { sessionId: 'd' }])
    const keys = map.nodes.value.map(n => `${n.x},${n.y}`)
    expect(new Set(keys).size).toBe(keys.length) // 无重叠
    expect(occupiedSlots(map.nodes.value).size).toBe(4)

    // 第一个在原点，第二个在紧邻格（方形螺旋由内向外，顺序确定 ⇒ 可预测）
    expect(map.nodes.value[0]).toMatchObject({ x: 0, y: 0 })
    expect(map.nodes.value[1]).toMatchObject({ x: -GRID, y: -GRID })
    expect(nextFreeSlot(new Set(['0,0']))).toEqual({ x: -GRID, y: -GRID })
  })

  it('相机：scale 被钳制；坐标与相机都会持久化', () => {
    const first = useSessionMap()
    first.importSessions([{ sessionId: 'a' }])
    first.addNote(520, 260, '便签')
    first.setCamera({ scale: 99, tx: 40, ty: -10 })

    expect(first.camera.value.scale).toBe(MAX_SCALE)
    expect(clampScale(0.0001)).toBe(MIN_SCALE)

    // 重新构造（模拟刷新页面）
    const second = useSessionMap()
    expect(second.nodes.value.length).toBe(2)
    expect(second.nodes.value[1].title).toBe('便签')
    expect(second.camera.value).toEqual({ scale: MAX_SCALE, tx: 40, ty: -10 })
  })

  it('存储损坏时降级为空地图，不抛错（不能拖垮会话列表）', () => {
    localStorage.setItem('chiron.sessionMap.v2', '{not json')
    expect(() => useSessionMap()).not.toThrow()
    expect(useSessionMap().nodes.value).toEqual([])
  })
})

describe('会话地图 —— 撤销 / 重做', () => {
  beforeEach(() => {
    installStorageStub()
  })

  it('拖动后可以撤销回原位', () => {
    const map = useSessionMap()
    map.addNote(0, 0, 'a')
    const id = map.nodes.value[0].id

    map.moveNode(id, 520, 260)
    expect(map.nodes.value[0].x).toBe(520)

    expect(map.undo()).toBe(true)
    expect(map.nodes.value[0].x).toBe(0)
  })

  it('★ 相机（平移/缩放）不进撤销栈 —— 否则撤销会变成"撤销视角"', () => {
    const map = useSessionMap()
    map.addNote(0, 0, 'a')
    map.undo() // 回到空地图，历史随之清空
    expect(map.canUndo.value).toBe(false)

    map.setCamera({ scale: 2, tx: 100, ty: 50 })
    map.setCamera({ scale: 1.5, tx: 0, ty: 0 })

    // 只动过相机 ⇒ 依然没有"内容"可撤销（相机不进历史）
    expect(map.canUndo.value).toBe(false)
    expect(map.undo()).toBe(false)
    // 但相机本身生效了
    expect(map.camera.value.scale).toBe(1.5)
  })

  it('重做可用；新操作会让重做失效', () => {
    const map = useSessionMap()
    map.addNote(0, 0, 'a')
    const id = map.nodes.value[0].id

    map.moveNode(id, 520, 260)
    map.undo()
    expect(map.canRedo.value).toBe(true)

    map.redo()
    expect(map.nodes.value[0].x).toBe(520)

    map.undo()
    map.setHidden(id, true) // 新操作
    expect(map.canRedo.value).toBe(false)
  })

  it('移除节点可撤销（这是最容易误触的操作）', () => {
    const map = useSessionMap()
    map.importSessions([{ sessionId: 'a' }, { sessionId: 'b' }])
    const victim = map.nodes.value[0]

    map.removeNode(victim.id)
    expect(map.nodes.value.length).toBe(1)

    map.undo()
    expect(map.nodes.value.length).toBe(2)
    expect(map.nodes.value.find(n => n.id === victim.id)).toBeTruthy()
  })

  it('历史有上限，不会无限增长', () => {
    const map = useSessionMap()
    map.addNote(0, 0, 'seed')
    const id = map.nodes.value[0].id
    for (let i = 1; i <= 80; i++) map.moveNode(id, i * GRID, 0)

    let steps = 0
    while (map.undo()) steps++
    expect(steps).toBeLessThanOrEqual(50)
  })
})
