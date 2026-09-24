/**
 * chat 域文案（源语言 zh-CN）。
 *
 * 相对时间用**命名插值**（`{n}`）而不是字符串拼接：语序在不同语言里不同
 * （`{n} 分钟前` / `{n} min ago` / `قبل {n} دقيقة`），拼接必然出错。
 */
export default {
  time: {
    justNow: '刚刚',
    minutesAgo: '{n} 分钟前',
    hoursAgo: '{n} 小时前',
    monthDay: '{month}月{day}日',
  },
  input: {
    modelTitle: '当前模型：{model}（仅影响后续消息）',
    modelDefault: '默认（后端路由）',
    stopSend: '停止生成（输入内容后按 Enter 可打断并发送）',
    send: '发送',
  },
  status: {
    online: '已连接',
    offline: '离线',
  },
  tabs: {
    runs: '运行',
    output: '输出',
    usage: '用量',
    artifacts: '产物',
    events: '事件流',
  },
  stats: {
    turns: '回合数',
    inputTokens: '输入 tokens',
    outputTokens: '输出 tokens',
    cachedTokens: '缓存命中 tokens',
    cacheHitRate: '缓存命中率',
    cost: '费用',
    ttftP50: '首字延迟 p50',
    outputTpsP50: '输出吞吐 p50',
    outputTpsP95: '输出吞吐 p95',
  },
  chip: {
    kb: '知识库 #{id}',
    skill: '技能 {name}',
    workflow: '工作流 {name}',
    plugin: '插件 {name}',
    memory: '记忆 {name}',
  },
  toolUnit: {
    lines: '行',
    codeLines: '行代码',
  },
  sessionmap: {
    /** 与 public/sessionmap/adapter.js 的 FOLLOWUP_RE 成对演进：这是**协议前缀**，
        刻意不随界面语言变化（en-US 同理），改它之前先确认 adapter 仍匹配。 */
    followup: '【请简短回答问题】:({text})',
    newSession: '新对话',
    serviceFailed: '会话服务调用失败',
    missingSource: '缺少来源会话',
    missingTarget: '缺少目标会话',
    emptyPatch: '没有要更新的字段',
    emptyMessage: '消息内容为空',
  },
  share: {
    defaultTitle: '对话记录',
    title: '分享「{name}」',
    newSession: '新对话',
    visibilityHint: '分享链接对任何获得链接的人可见',
    selectMessages: '选择要分享的消息（{selected}/{total}）',
    roleUser: '用户',
    emptyMessage: '（空消息）',
  },
  agent: {
    createHint: '会话正文（{count} 字符）会作为它的系统提示词；创建后可在 Agents 页继续调整提示词、工具与工作台绑定。',
  },
}
