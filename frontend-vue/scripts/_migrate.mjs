import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..') // frontend-vue/
const locales = path.join(root, 'src', 'locales')
const langs = ['zh-CN', 'en-US', 'ar']

const read = (p) => fs.readFileSync(p, 'utf8')
const write = (p, s) => fs.writeFileSync(p, s, 'utf8')

// ---- parse 'KEY': 'VAL' lines (VAL may be single- or double-quoted) ----
const ENTRY_RE = /^\s*'((?:\\.|[^'\\])*)'\s*:\s*('(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")\s*,?\s*$/gm
function unquoteVal(s) {
  if (s.length >= 2 && (s[0] === "'" || s[0] === '"')) {
    return s.slice(1, -1).replace(/\\'/g, "'").replace(/\\"/g, '"')
  }
  return s
}
function parseEntries(text) {
  const m = new Map()
  let r
  ENTRY_RE.lastIndex = 0
  while ((r = ENTRY_RE.exec(text))) m.set(r[1], unquoteVal(r[2]))
  return m
}
// all object-key identifiers in a file (any indent) — for dedupe
const KEYNAME_RE = /^[ \t]*([A-Za-z_$][\w$]*)\s*:/gm
function parseKeyNames(text) {
  const s = new Set()
  let r
  KEYNAME_RE.lastIndex = 0
  while ((r = KEYNAME_RE.exec(text))) s.add(r[1])
  return s
}

const zh = parseEntries(read(path.join(locales, 'zh-CN', 'legacy.ts')))
const en = parseEntries(read(path.join(locales, 'en-US', 'legacy.ts')))
const ar = parseEntries(read(path.join(locales, 'ar', 'legacy.ts')))

// ---- existing semantic keys (for reuse-by-value + dedupe) ----
// common block (inline in index.ts)
function parseCommonBlock(text) {
  const m = text.match(/common:\s*\{([\s\S]*?)\n  \},/)
  if (!m) return new Map()
  return parseEntries(m[1])
}
const existingByValue = new Map() // zhVal -> newKey
const usedSlugs = new Map() // domain -> Set(slug)
function domainInit(d) {
  if (!usedSlugs.has(d)) usedSlugs.set(d, new Set())
  return usedSlugs.get(d)
}

// seed common from inline block (all 3 langs share identical zh-CN values)
for (const lang of langs) {
  const blk = parseCommonBlock(read(path.join(locales, lang, 'index.ts')))
  for (const [k, v] of blk) {
    existingByValue.set(v, `common.${k}`)
    domainInit('common').add(k)
  }
}
// seed domain files (chat/errors/auth/admin): existing key NAMES (dedupe) + zh-CN values (reuse)
for (const d of ['chat', 'errors', 'auth', 'admin']) {
  const txt = read(path.join(locales, 'zh-CN', d + '.ts'))
  const names = parseKeyNames(txt)
  const used = domainInit(d)
  for (const n of names) used.add(n)
  const entries = parseEntries(txt)
  for (const [k, v] of entries) existingByValue.set(v, `${d}.${k}`)
}

// ---- classifier ----
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
  ['common', []] // catch-all
]
function classify(key, enVal) {
  const e = (enVal || '').toLowerCase()
  for (const [domain, kws] of RULES) {
    if (domain === 'common') continue
    for (const kw of kws) if (e.includes(kw) || key.includes(kw)) return domain
  }
  return 'common'
}

function slugify(s) {
  if (!s) return ''
  return s
    .toLowerCase()
    .replace(/^[^a-z0-9]+/, '') // strip leading non-alnum (e.g. "# ", "## ", "$ ")
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
}
// JS object-literal key must be quoted if it isn't a valid identifier (e.g. starts with a digit)
const IDENT_RE = /^[A-Za-z_$][A-Za-z0-9_$]*$/
function keyRep(slug) {
  return IDENT_RE.test(slug) ? slug : `'${slug}'`
}
function hashSlug(s) {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0
  return 'k' + (h >>> 0).toString(36)
}

// ---- build the migration map ----
const callMap = new Map() // chineseKey -> newKey
const newEntries = new Map() // `${domain}|${lang}` -> Map(slug -> value)
function bucket(domain, lang) {
  const k = `${domain}|${lang}`
  if (!newEntries.has(k)) newEntries.set(k, new Map())
  return newEntries.get(k)
}

let reused = 0
let created = 0
let emptyEn = 0
for (const [key] of zh) {
  if (existingByValue.has(key)) {
    callMap.set(key, existingByValue.get(key))
    reused++
    continue
  }
  const enVal = en.get(key)
  let domain = classify(key, enVal)
  let base = slugify(enVal)
  if (!base) {
    base = hashSlug(key)
    if (!enVal) emptyEn++
  }
  const used = domainInit(domain)
  let slug = base
  let i = 2
  while (used.has(slug)) slug = `${base}_${i++}`
  used.add(slug)
  const newKey = `${domain}.${slug}`
  callMap.set(key, newKey)
  created++
  for (const lang of langs) {
    const val = lang === 'zh-CN' ? key : (lang === 'en-US' ? (enVal ?? '') : (ar.get(key) ?? ''))
    bucket(domain, lang).set(slug, val)
  }
}

// ---- 1) strip migrated keys from legacy.ts (3 langs) ----
const migratedSet = new Set(zh.keys())
for (const lang of langs) {
  const p = path.join(locales, lang, 'legacy.ts')
  const lines = read(p).split('\n')
  const out = []
  for (const line of lines) {
    const m = line.match(/^\s*'((?:\\.|[^'\\])*)'\s*:\s*('(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")\s*,?\s*$/)
    if (m && migratedSet.has(m[1])) continue
    out.push(line)
  }
  write(p, out.join('\n'))
}

// ---- 2) write new entries into domain files / common block ----
const EXISTING_FILE_DOMAINS = new Set(['chat', 'errors', 'auth', 'admin'])
const NEW_FILE_DOMAINS = ['workflow', 'agent', 'knowledge', 'mail', 'memory', 'media', 'billing', 'settings']

function appendToFile(p, entriesMap) {
  let txt = read(p)
  let body = [...entriesMap.entries()].map(([slug, val]) => `  ${keyRep(slug)}: ${JSON.stringify(val)},`).join('\n')
  // insert before the final closing brace of `export default {`
  const idx = txt.lastIndexOf('}')
  txt = txt.slice(0, idx) + (txt.slice(0, idx).endsWith('\n') ? '' : '\n') + body + '\n' + txt.slice(idx)
  write(p, txt)
}
function createFile(p, entriesMap) {
  const body = [...entriesMap.entries()].map(([slug, val]) => `  ${keyRep(slug)}: ${JSON.stringify(val)},`).join('\n')
  write(p, `// 从 legacy 迁移的语义化 key（L1-4）\nexport default {\n${body}\n}\n`)
}
function appendToCommonBlock(p, entriesMap) {
  let txt = read(p)
  const body = [...entriesMap.entries()].map(([slug, val]) => `    ${keyRep(slug)}: ${JSON.stringify(val)},`).join('\n')
  txt = txt.replace(/common:\s*\{([\s\S]*?)\n  \},/, (m, inner) => `common: {\n${inner}${inner.endsWith('\n') ? '' : '\n'}${body}\n  },`)
  write(p, txt)
}

const domainsAll = [...EXISTING_FILE_DOMAINS, 'common', ...NEW_FILE_DOMAINS]
for (const domain of domainsAll) {
  for (const lang of langs) {
    const entries = bucket(domain, lang)
    if (entries.size === 0) continue
    if (domain === 'common') {
      appendToCommonBlock(path.join(locales, lang, 'index.ts'), entries)
    } else if (EXISTING_FILE_DOMAINS.has(domain)) {
      appendToFile(path.join(locales, lang, domain + '.ts'), entries)
    } else {
      const p = path.join(locales, lang, domain + '.ts')
      if (fs.existsSync(p)) appendToFile(p, entries)
      else createFile(p, entries)
    }
  }
}

// ---- 3) index.ts: import + export-ref new domain files (3 langs) ----
for (const lang of langs) {
  const p = path.join(locales, lang, 'index.ts')
  let txt = read(p)
  // add import lines after admin import
  let importBlock = ''
  for (const d of NEW_FILE_DOMAINS) {
    const imp = `import ${d} from './${d}'`
    if (!txt.includes(imp)) importBlock += imp + '\n'
  }
  if (importBlock) txt = txt.replace(/(import admin from '.\/admin'\n)/, `$1${importBlock}`)
  // add export refs after `admin,`
  let exportBlock = ''
  for (const d of NEW_FILE_DOMAINS) {
    if (!txt.includes(`  ${d},`)) exportBlock += `  ${d},\n`
  }
  if (exportBlock) txt = txt.replace(/(\n  admin,\n)/, `$1${exportBlock}`)
  write(p, txt)
}

// ---- 4) rewrite call sites ----
const CALL_RE = /(\$t|\bte(?![A-Za-z])|\btc(?![A-Za-z])|\bt(?![A-Za-z])|hasMessage)\s*\(\s*(['"`])((?:\\.|[^'"`\\])*)\2\s*\)/g
const srcDir = path.join(root, 'src')
let filesTouched = 0
let callsRewritten = 0
function walk(dir) {
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const fp = path.join(dir, ent.name)
    if (ent.isDirectory()) {
      if (ent.name === 'locales' || ent.name === 'node_modules') continue
      walk(fp)
    } else if (/\.(vue|ts|js)$/.test(ent.name)) {
      if (ent.name.startsWith('_migrate') || ent.name.startsWith('_analyze')) continue
      let txt = read(fp)
      let changed = false
      txt = txt.replace(CALL_RE, (m, call, q, arg) => {
        const nk = callMap.get(arg)
        if (!nk) return m
        changed = true
        callsRewritten++
        return `${call}(${q}${nk}${q})`
      })
      if (changed) { write(fp, txt); filesTouched++ }
    }
  }
}
walk(srcDir)

// ---- report ----
const out = {
  total: zh.size, reused, created, emptyEn,
  filesTouched, callsRewritten,
  perDomain: {},
}
for (const [k, v] of newEntries) {
  const [d, lang] = k.split('|')
  out.perDomain[d] = out.perDomain[d] || {}
  out.perDomain[d][lang] = v.size
}
write(path.join(root, 'scripts', '_migrate_report.json'), JSON.stringify(out, null, 2))
console.log(JSON.stringify(out, null, 2))
