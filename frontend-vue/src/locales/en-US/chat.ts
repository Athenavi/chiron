/**
 * chat domain strings (en-US). Keys must match zh-CN/chat.ts.
 */
export default {
  time: {
    justNow: 'Just now',
    minutesAgo: '{n} min ago',
    hoursAgo: '{n} h ago',
    monthDay: '{month}/{day}',
  },
  input: {
    modelTitle: 'Current model: {model} (affects subsequent messages only)',
    modelDefault: 'default (backend routing)',
    stopSend: 'Stop generating (press Enter to interrupt and send)',
    send: 'Send',
  },
  status: {
    online: 'Connected',
    offline: 'Offline',
  },
  tabs: {
    runs: 'Runs',
    output: 'Output',
    usage: 'Usage',
    artifacts: 'Artifacts',
    events: 'Events',
  },
  stats: {
    turns: 'Turns',
    inputTokens: 'Input tokens',
    outputTokens: 'Output tokens',
    cachedTokens: 'Cached tokens',
    cacheHitRate: 'Cache hit rate',
    cost: 'Cost',
    ttftP50: 'TTFT p50',
    outputTpsP50: 'Output tok/s p50',
    outputTpsP95: 'Output tok/s p95',
  },
  chip: {
    kb: 'Knowledge base #{id}',
    skill: 'Skill {name}',
    workflow: 'Workflow {name}',
    plugin: 'Plugin {name}',
    memory: 'Memory {name}',
  },
  toolUnit: {
    lines: 'lines',
    codeLines: 'lines of code',
  },
  sessionmap: {
    /** 见 zh-CN 同名注释：协议前缀，不随语言变化。 */
    followup: '【请简短回答问题】:({text})',
    newSession: 'New chat',
    serviceFailed: 'Session service call failed',
    missingSource: 'Missing source session',
    missingTarget: 'Missing target session',
    emptyPatch: 'Nothing to update',
    emptyMessage: 'Message is empty',
  },
  share: {
    defaultTitle: 'Conversation',
    title: 'Share "{name}"',
    newSession: 'New chat',
    visibilityHint: 'Anyone with the link can view this share',
    selectMessages: 'Select messages to share ({selected}/{total})',
    roleUser: 'User',
    emptyMessage: '(empty message)',
  },
  agent: {
    createHint: 'The conversation body ({count} chars) becomes its system prompt; you can refine the prompt, tools and workstation binding later on the Agents page.',
  },
}
