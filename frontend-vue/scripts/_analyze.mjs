import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..') // frontend-vue/
const locales = path.join(root, 'src', 'locales')

// ---- parse legacy.ts (gettext: KEY === zh-CN source, value === translation) ----
function parseLegacy(file) {
  const text = fs.readFileSync(file, 'utf8')
  const re = /^\s*'((?:\\.|[^'\\])*)'\s*:\s*'((?:\\.|[^'\\])*)',?\s*$/gm
  const out = new Map()
  let m
  while ((m = re.exec(text))) out.set(m[1], m[2])
  return out
}

const zh = parseLegacy(path.join(locales, 'zh-CN', 'legacy.ts'))
const en = parseLegacy(path.join(locales, 'en-US', 'legacy.ts'))
const ar = parseLegacy(path.join(locales, 'ar', 'legacy.ts'))

console.log('zh-CN legacy entries:', zh.size)
console.log('en-US legacy entries:', en.size)
console.log('ar  legacy entries:', ar.size)

// ---- domain classifier (keyword rules; English value primary, Chinese fallback) ----
const RULES = [
  ['auth', ['login', 'register', 'logout', 'password', 'verification', 'captcha', 'account', 'sign in', 'sign up', 'reset', 'username', '登录', '注册', '密码', '验证码', '账号', '用户名', '退出', '登出']],
  ['errors', ['fail', 'error', 'exception', 'invalid', 'timeout', 'exceed', 'insufficient', 'not found', 'denied', 'conflict', 'expired', 'format', 'length', 'required', 'validate', 'quota', '失败', '错误', '异常', '无效', '超时', '超出', '不足', '不存在', '未找到', '权限', '拒绝', '冲突', '过期', '格式', '长度', '必填', '校验']],
  ['billing', ['payment', 'subscription', 'order', 'plan', 'invoice', 'billing', 'balance', 'price', 'credit', '积分', '支付', '订阅', '订单', '套餐', '发票', '费用', '额度', '余额', '账单', '价格', '优惠']],
  ['workflow', ['workflow', 'dag', 'node', 'trigger', 'schedule', 'pipeline', 'step', '编排', '工作流', '节点', '任务', '触发器', '定时', '流程']],
  ['agent', ['agent', 'tool', 'skill', 'mcp', 'model', 'prompt', 'parameter', '智能体', '代理', '工具', '技能', '模型', '提示词', '参数']],
  ['knowledge', ['knowledge', 'document', 'retrieval', 'vector', 'embedding', 'chunk', 'corpus', '知识库', '文档', '检索', '向量', '切片', '语料']],
  ['mail', ['mail', 'email', 'smtp', 'inbox', '收件', '发信', '邮件']],
  ['memory', ['memory', 'note', '记忆', '笔记']],
  ['media', ['image', 'video', 'audio', 'attachment', '图片', '视频', '音频', '附件', '文件']],
  ['chat', ['chat', 'message', 'conversation', 'session', 'reply', 'thinking', 'reasoning', 'transcript', '会话', '对话', '消息', '聊天', '回复', '推理', '思考', '转写']],
  ['admin', ['user', 'role', 'permission', 'tenant', 'audit', 'monitor', 'cache', 'queue', 'database', 'redis', 'domain', 'group', 'identity', 'governance', 'policy', 'route', 'performance', 'privacy', '用户', '角色', '租户', '审计', '监控', '缓存', '队列', '数据库', '域名', '分组', '身份', '治理', '策略', '路由', '性能', '隐私']],
  ['settings', ['setting', 'preference', 'profile', 'notification', 'theme', 'appearance', '设置', '偏好', '个人', '通知', '主题', '外观', '账户']],
  ['common', ['save', 'delete', 'confirm', 'cancel', 'search', 'refresh', 'load', 'success', 'please', 'whether', 'whether', 'all', 'add', 'edit', 'operation', 'tip', 'warning', 'notice', 'close', 'copy', 'export', 'import', 'upload', 'download', 'submit', 'clear', 'reset', 'name', 'description', 'status', 'type', 'time', 'date', 'count', 'empty', 'processing', 'complete', 'start', 'stop', 'run', 'enable', 'disable', 'show', 'hide', 'expand', 'collapse', 'more', 'back', 'next', 'prev', 'select', 'filter', 'sort', 'list', 'detail', 'view', 'preview', 'open', '保存', '删除', '确认', '取消', '搜索', '刷新', '加载', '成功', '请', '是否', '全部', '新增', '编辑', '操作', '提示', '警告', '注意', '关闭', '复制', '导出', '导入', '上传', '下载', '提交', '清空', '重置', '名称', '描述', '状态', '类型', '时间', '日期', '数量', '暂无', '加载中', '处理中', '完成', '开始', '停止', '运行', '启用', '禁用', '显示', '隐藏', '展开', '收起', '更多', '返回', '下一步', '上一步', '选择', '筛选', '排序', '列表', '详情', '查看', '预览', '打开']],
]

function classify(key, enVal) {
  const e = (enVal || '').toLowerCase()
  const c = key // Chinese key
  for (const [domain, kws] of RULES) {
    for (const kw of kws) {
      if (e.includes(kw) || c.includes(kw)) return domain
    }
  }
  return 'common'
}

const buckets = new Map()
for (const [k] of zh) {
  const d = classify(k, en.get(k))
  if (!buckets.has(d)) buckets.set(d, [])
  buckets.get(d).push(k)
}

console.log('\n=== per-domain counts ===')
const sorted = [...buckets.entries()].sort((a, b) => b[1].length - a[1].length)
for (const [d, ks] of sorted) console.log(`${d.padEnd(12)} ${ks.length}`)

console.log('\n=== common samples (catch-all sanity) ===')
for (const k of (buckets.get('common') || []).slice(0, 40)) console.log('  ', k, '=>', en.get(k))

console.log('\n=== keys with empty en-US value ===')
let empty = 0
for (const [k] of zh) if (!en.get(k)) { empty++; if (empty <= 10) console.log('  ', k) }
console.log('  total empty:', empty)
