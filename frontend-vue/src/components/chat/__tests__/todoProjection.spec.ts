import { describe, expect, it } from 'vitest'

import {
  boardFromHistory,
  EMPTY_BOARD,
  normalizeStatus,
  projectTodoBoard,
  shouldApply,
  summarize,
  type TodoRow,
} from '../todoProjection'

/**
 * 投影层是纯函数，因此这里不做组件挂载 —— 面板渲染再怎么调，只要投影对，
 * 数据就是对的。反过来说，投影错了就算组件一像素不差也是错的。
 */
describe('todoProjection（任务清单投影）', () => {
  it('归一化只认三种规范状态，其余一律 pending', () => {
    expect(normalizeStatus('in_progress')).toBe('in_progress')
    expect(normalizeStatus('completed')).toBe('completed')
    expect(normalizeStatus('pending')).toBe('pending')
    // 未知值不能抛错：模板必须总能渲染，否则一条脏数据能让整个面板白掉
    expect(normalizeStatus('banana')).toBe('pending')
    expect(normalizeStatus(undefined)).toBe('pending')
    expect(normalizeStatus(null)).toBe('pending')
  })

  it('按 id 给出稳定编号，缺 id 时用序号兜底', () => {
    const board = projectTodoBoard([
      { id: 't7', content: 'a' },
      { content: 'b' },
      { id: 'weird', content: 'c' },
    ])
    expect(board.rows.map((r) => r.index)).toEqual([7, 2, 3])
    // id 必须原样保留：面板 key 与"按编号更新"都依赖它
    expect(board.rows.map((r) => r.id)).toEqual(['t7', 't2', 'weird'])
  })

  it('统计与百分比：空板返回 0 而不是 NaN', () => {
    expect(summarize([])).toEqual({
      total: 0,
      pending: 0,
      inProgress: 0,
      completed: 0,
      percent: 0,
    })
    const rows = [
      { status: 'completed' },
      { status: 'completed' },
      { status: 'in_progress' },
      { status: 'pending' },
    ] as TodoRow[]
    expect(summarize(rows)).toEqual({
      total: 4,
      pending: 1,
      inProgress: 1,
      completed: 2,
      percent: 50,
    })
  })

  it('依赖未完成时标记阻塞，并列出在等谁', () => {
    const board = projectTodoBoard([
      { id: 't1', content: 'a', status: 'pending' },
      { id: 't2', content: 'b', status: 'pending', depends_on: ['t1'] },
    ])
    expect(board.rows[0].blocked).toBe(false)
    expect(board.rows[1].blocked).toBe(true)
    expect(board.rows[1].blockedBy).toEqual(['t1'])
  })

  it('依赖完成后不再阻塞', () => {
    const board = projectTodoBoard([
      { id: 't1', content: 'a', status: 'completed' },
      { id: 't2', content: 'b', status: 'pending', depends_on: ['t1'] },
    ])
    expect(board.rows[1].blocked).toBe(false)
    expect(board.rows[1].blockedBy).toEqual([])
  })

  it('已完成的项即使依赖没完成也不标阻塞', () => {
    // 否则做完的条目会一直挂着"等待前序"，看起来像没收尾
    const board = projectTodoBoard([
      { id: 't1', content: 'a', status: 'pending' },
      { id: 't2', content: 'b', status: 'completed', depends_on: ['t1'] },
    ])
    expect(board.rows[1].blocked).toBe(false)
  })

  it('依赖成环时两边都算阻塞，但不死循环', () => {
    const board = projectTodoBoard([
      { id: 't1', content: 'a', depends_on: ['t2'] },
      { id: 't2', content: 'b', depends_on: ['t1'] },
    ])
    expect(board.rows.every((r) => r.blocked)).toBe(true)
  })

  it('收集认领者去重，供面板判断要不要显示归属', () => {
    const board = projectTodoBoard([
      { id: 't1', content: 'a', owner: 'main' },
      { id: 't2', content: 'b', owner: 'run-42' },
      { id: 't3', content: 'c', owner: 'main' },
      { id: 't4', content: 'd' },
    ])
    expect(board.owners).toEqual(['main', 'run-42'])
  })

  it('脏输入不抛错：非数组、非对象条目、缺字段都被滤掉', () => {
    expect(projectTodoBoard(null).rows).toEqual([])
    expect(projectTodoBoard(undefined).rows).toEqual([])
    expect(projectTodoBoard('nonsense').rows).toEqual([])
    expect(projectTodoBoard([null, 1, 'x']).rows).toEqual([])
    // 缺 content 也保留（编号仍需存在），只是文案为空
    expect(projectTodoBoard([{ id: 't1' }]).rows[0].content).toBe('')
  })

  it('revision 单调：旧事件被丢弃，缺 revision 的旧数据总是采用', () => {
    expect(shouldApply({ revision: 3 }, 2)).toBe(true)
    expect(shouldApply({ revision: 2 }, 2)).toBe(false)
    expect(shouldApply({ revision: 1 }, 2)).toBe(false)
    // 缺 revision（旧引擎/旧会话）：采用，否则升级后老面板永远空白
    expect(shouldApply({}, 99)).toBe(true)
    expect(shouldApply({ revision: undefined }, 99)).toBe(true)
  })

  it('空板是可直接渲染的常量', () => {
    expect(EMPTY_BOARD.rows).toEqual([])
    expect(EMPTY_BOARD.summary.total).toBe(0)
  })
})

/**
 * 历史投影：`todo_updated` 是实时事件、刷新后不会重放，所以必须能从落库的
 * `todo_write` 工具调用里把清单捞回来 —— 否则刷新一下长任务面板就空了，
 * 而模型那边仍有清单（它读的是 tool 消息），两边从此不一致。
 */
describe('boardFromHistory（刷新后从历史恢复清单）', () => {
  it('从最后一条 todo_write 恢复，而不是最早那条', () => {
    // 全量覆写语义下，最新那份才是当前真相
    const board = boardFromHistory([
      { tool_name: 'todo_write', output: JSON.stringify({ todos: [{ content: '旧', status: 'pending' }] }) },
      { tool_name: 'read_file', output: 'irrelevant' },
      { tool_name: 'todo_write', output: JSON.stringify({ todos: [{ content: '新', status: 'completed' }] }) },
    ])
    expect(board.rows).toHaveLength(1)
    expect(board.rows[0].content).toBe('新')
    expect(board.rows[0].status).toBe('completed')
  })

  it('忽略其它工具的输出（不去猜哪个工具有清单）', () => {
    const board = boardFromHistory([
      { tool_name: 'bash', output: JSON.stringify({ todos: [{ content: '假的', status: 'pending' }] }) },
    ])
    expect(board.rows).toEqual([])
  })

  it('被截断的 JSON 跳过，继续找更早的完整记录', () => {
    // output_truncated 是常态：一条坏记录不该让整个面板空掉
    const board = boardFromHistory([
      { tool_name: 'todo_write', output: JSON.stringify({ todos: [{ content: '完整', status: 'pending' }] }) },
      { tool_name: 'todo_write', output: '{"todos": [{"content": "截断的记' },
    ])
    expect(board.rows[0].content).toBe('完整')
  })

  it('全部不可解析时返回空板而不是抛错', () => {
    expect(boardFromHistory([{ tool_name: 'todo_write', output: 'not json' }]).rows).toEqual([])
    expect(boardFromHistory([]).rows).toEqual([])
    expect(boardFromHistory(null).rows).toEqual([])
    expect(boardFromHistory(undefined).rows).toEqual([])
  })

  it('恢复时保留认领者与依赖（不因走历史而丢协作信息）', () => {
    const board = boardFromHistory([
      {
        tool_name: 'todo_write',
        output: JSON.stringify({
          todos: [
            { id: 't1', content: 'a', status: 'completed', owner: 'main' },
            { id: 't2', content: 'b', status: 'pending', owner: 'run-1', depends_on: ['t1'] },
          ],
        }),
      },
    ])
    expect(board.owners).toEqual(['main', 'run-1'])
    expect(board.rows[1].dependsOn).toEqual(['t1'])
    expect(board.rows[1].blocked).toBe(false) // t1 已完成
  })
})
