/**
 * 把一次对话沉淀成可复用的工作流图定义。
 *
 * 产物直接对接 `POST /v1/graphs`（graph_json 存进 workflow_graphs，再由
 * `POST /v1/graphs/{id}/execute` 或 queue worker 执行）。执行器读的是
 * `node.node_type` 与 `node.config`（见 python-engine/app/workflow/engine.py）。
 *
 * 两种形态：
 *
 * 1. **没有知识库** —— 单个 llm 节点，整段对话正文喂进 `user_message`。
 *    为什么是单节点：对话本身不是 DAG，硬拆成节点链等于替用户猜"哪句是条件、
 *    哪步是工具"。先做到"这段对话可以被重复执行"，节点粒度留给用户在 DAG 编辑器里调。
 *
 * 2. **挂了知识库** —— `input → knowledge → llm` 三点链（见下方 sessionToGraph 的注释）。
 *
 * 一个容易踩的前提：这几处"留空"依赖运行时输入（`initial_state.input`）。
 * `_input_node` 在没有输入时会退回 `"[input] {label}"` 占位串，届时知识库检索会用
 * 那个占位串当 query。前端 WorkflowView 现在会在图含 input 节点时向用户收集输入。
 */

import type { ChatItem, TextItem } from '../components/chat/chat-types'
import { sessionToMarkdown } from './sessionMarkdown'

import { t } from '../i18n'
/** 后端 graph_json 的节点形态（与 `WorkflowView.toBackendFormat` 一致） */
export interface GraphNodeBackend {
  id: string
  label: string
  node_type: string
  config: Record<string, unknown>
}

/** 后端 graph_json 的连线形态（与 `WorkflowView.toBackendFormat` 一致） */
export interface GraphEdgeBackend {
  source_id: string
  target_id: string
  condition?: string
  label?: string
}

export interface GraphDefinition {
  name: string
  nodes: GraphNodeBackend[]
  edges: GraphEdgeBackend[]
  entry_point: string
}

/** 单节点形态的固定 id：没有连线，入口就是它自己 */
const SOLE_NODE_ID = 'n1'

/** 带知识库时的三点链节点 id */
const INPUT_NODE_ID = 'n_input'
const KNOWLEDGE_NODE_ID = 'n_kb'
/** llm 节点的 id —— 两种形态都用它，便于用户事后在编辑器里识别 */
const LLM_NODE_ID = SOLE_NODE_ID

/** 给 llm 节点的 system_prompt —— 交代这段正文的来历，而不是让它当普通提问回答 */
const GRAPH_SYSTEM_PROMPT = () => t('下面是此前的一段对话记录。请把它当作背景，在此基础上继续完成其中的工作。')

export interface SessionGraph {
  name: string
  graph_json: GraphDefinition
}

export interface SessionGraphOptions {
  /** 对话里挂的知识库 id；有值时生成 input → knowledge → llm 链 */
  kbId?: string
}

/**
 * 会话 → 工作流图。正文为空时返回 null，调用方据此禁用入口。
 *
 * 不能只看 `sessionToMarkdown` 的返回值来判断空：它带上标题后，即使一条正文都没有
 * 也会返回 `# 标题`（非空），那样会产出一个只有标题的空壳工作流。所以这里单独查正文。
 */
export function sessionToGraph(
  items: readonly ChatItem[],
  title?: string,
  options: SessionGraphOptions = {},
): SessionGraph | null {
  const hasBody = items.some(
    item => item.kind === 'text' && String((item as TextItem).content ?? '').trim().length > 0,
  )
  if (!hasBody) return null

  const markdown = sessionToMarkdown(items, title)
  const name = (title || '').trim() || t('对话工作流 {time}', { time: new Date().toLocaleString() })
  const kbId = (options.kbId || '').trim()

  // ── 没挂知识库：保持单 llm 节点，正文进 user_message ──
  if (!kbId) {
    return {
      name,
      graph_json: {
        name,
        nodes: [
          {
            id: SOLE_NODE_ID,
            label: name,
            node_type: 'llm',
            config: {
              system_prompt: GRAPH_SYSTEM_PROMPT(),
              user_message: markdown,
              model: '',
            },
          },
        ],
        edges: [],
        entry_point: SOLE_NODE_ID,
      },
    }
  }

  // ── 挂了知识库：input → knowledge → llm ──
  //
  // 两处"留空"都是刻意的，对应引擎的读取顺序（engine.py）：
  // - `knowledge.query` 留空 → `_knowledge_node` 落到 `_prev_output`（= 运行时输入）
  // - `llm.user_message` 留空 → `_llm_node` 落到 `_prev_output`（= 检索片段）
  // 因此对话正文必须挪进 llm 的 `system_prompt`：它原来占着 user_message 的位置，
  // 而那个位置现在要留给检索片段。
  //
  // 为什么不把 kb_id 直接塞进 llm 节点的 config：`_llm_node` 根本不读这个键
  // （它只认 system_prompt / user_message / model），那样写会是一个静默无效的字段。
  return {
    name,
    graph_json: {
      name,
      nodes: [
        {
          id: INPUT_NODE_ID,
          label: t('输入'),
          node_type: 'input',
          config: {},
        },
        {
          id: KNOWLEDGE_NODE_ID,
          label: t('知识库检索'),
          node_type: 'knowledge',
          config: {
            kb_id: kbId,
            query: '',
            top_k: 5,
          },
        },
        {
          id: LLM_NODE_ID,
          label: name,
          node_type: 'llm',
          config: {
            system_prompt: `${GRAPH_SYSTEM_PROMPT()}\n\n${markdown}`,
            user_message: '',
            model: '',
          },
        },
      ],
      edges: [
        { source_id: INPUT_NODE_ID, target_id: KNOWLEDGE_NODE_ID },
        { source_id: KNOWLEDGE_NODE_ID, target_id: LLM_NODE_ID },
      ],
      entry_point: INPUT_NODE_ID,
    },
  }
}
