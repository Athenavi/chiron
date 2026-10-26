#!/usr/bin/env node
/**
 * 从 git 历史生成版本变更说明（Release body）。
 *
 * 用法：
 *   node .github/scripts/changelog.mjs <当前 tag> [上一个 tag]
 *   node .github/scripts/changelog.mjs v0.1.261025-01
 *
 * 不传上一个 tag 时，按语义化版本排序自动找（git tag --sort=-v:refname）。
 * 仓库里一个 tag 都没有时，退化为「取最近 N 条提交」。
 *
 * 输出 Markdown 到 stdout，便于 `> changelog.md` 或 `>> "$GITHUB_OUTPUT"`。
 *
 * 为什么不用 GitHub 自带的 generate_release_notes：
 *   它基于 PR 标题与合并记录，而 Chiron 的提交习惯是 Conventional Commits。
 *   自己解析 git log 能拿到 type(scope) 分类与 BREAKING CHANGE，内容更贴实际改动。
 */
import { execFileSync } from 'node:child_process'

const N_RECENT = 30 // 无 tag 时最多取多少条

const GROUPS = [
  { key: 'feat', title: '新功能' },
  { key: 'fix', title: '修复' },
  { key: 'perf', title: '性能' },
  { key: 'refactor', title: '重构' },
  { key: 'docs', title: '文档' },
  { key: 'test', title: '测试' },
  { key: 'build', title: '构建' },
  { key: 'ci', title: 'CI / 持续集成' },
  { key: 'style', title: '样式' },
  { key: 'chore', title: '杂项' },
]
const KNOWN = new Set(GROUPS.map((g) => g.key))

function git(args, opts = {}) {
  return execFileSync('git', args, { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, ...opts }).trim()
}

function listTags() {
  try {
    return git(['tag', '--sort=-v:refname']).split('\n').filter(Boolean)
  } catch {
    return []
  }
}

function findPrevTag(current) {
  const tags = listTags()
  const i = tags.indexOf(current)
  if (i >= 0) return tags[i + 1] ?? null // 排序后的下一个就是上一版
  return tags.find((t) => t !== current) ?? null
}

/** 解析 `git log` 输出：每条记录以 \x1e 结尾，字段以 \x00 分隔 */
function readCommits(rangeArgs) {
  const fmt = ['--pretty=format:%H%x00%s%x00%b%x1e', '--no-merges']
  const raw = execFileSync('git', ['log', ...fmt, ...rangeArgs], {
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  })
  return raw
    .split('\x1e')
    .map((r) => r.trim())
    .filter(Boolean)
    .map((rec) => {
      const [hash = '', subject = '', body = ''] = rec.split('\x00')
      return { hash: hash.trim(), subject: subject.trim(), body: body.trim() }
    })
}

const CONVENTIONAL = /^(\w+)(?:\(([^)]+)\))?(!)?:\s*(.+)$/

/**
 * GitHub 会把 Release 正文里的裸 `@name` 渲染成用户提及，链到
 * https://github.com/<name>。提交标题里出现「改用 @master」这类文字时，
 * 发布说明就会多出一个指向不存在用户的“贡献者”链接。
 * 用行内代码包住裸 mention：GitHub 不解析 code span 里的 @。
 * 邮箱（user@example.com）的 @ 前是单词字符，不会命中。
 */
function escapeMentions(text) {
  return text.replace(/(^|[^\w`])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/g, '$1`@$2`')
}

function classify(commit) {
  const m = commit.subject.match(CONVENTIONAL)
  const isBreaking = /(^|\n)BREAKING[ -]CHANGE:/.test(commit.body) || Boolean(m?.[3])
  if (!m) return { type: null, scope: null, desc: commit.subject, isBreaking }
  const [, type, scope = null, , desc] = m
  return { type, scope, desc, isBreaking }
}

function main() {
  const current = process.argv[2] || process.env.GITHUB_REF_NAME || ''
  if (!current) {
    console.error('用法: node changelog.mjs <当前 tag> [上一个 tag]')
    process.exit(1)
  }
  const prev = process.argv[3] || findPrevTag(current)

  const commits = prev ? readCommits([`${prev}..${current}`]) : readCommits([`-${N_RECENT}`])

  const buckets = new Map(GROUPS.map((g) => [g.key, []]))
  const breaking = []
  const other = []

  for (const c of commits) {
    const info = classify(c)
    const item = { ...c, ...info }
    if (info.isBreaking) breaking.push(item)
    if (info.type && KNOWN.has(info.type)) buckets.get(info.type).push(item)
    else other.push(item)
  }

  const out = []
  const rangeLabel = prev ? `${prev} → ${current}` : `${current}（仓库首个版本，取最近 ${commits.length} 条提交）`
  out.push(`**变更范围**：\`${rangeLabel}\``, '')
  out.push(`**提交数**：${commits.length}`, '')

  const section = (title, items, withType = false) => {
    if (!items.length) return
    out.push(`#### ${title}`, '')
    for (const it of items) {
      const scope = it.scope ? `**${it.scope}**: ` : ''
      const typePrefix = withType && it.type ? `\`${it.type}\` ` : ''
      const short = it.hash.slice(0, 7)
      out.push(`- ${typePrefix}${scope}${escapeMentions(it.desc)}（\`${short}\`）`)
    }
    out.push('')
  }

  if (breaking.length) {
    out.push('#### ⚠️ 破坏性变更', '')
    for (const it of breaking) {
      const scope = it.scope ? `**${it.scope}**: ` : ''
      out.push(`- ${scope}${escapeMentions(it.desc)}（\`${it.hash.slice(0, 7)}\`）`)
    }
    out.push('')
  }

  for (const g of GROUPS) section(g.title, buckets.get(g.key))
  section('其他', other, true)

  if (!commits.length) {
    out.push('_本次没有新的提交记录（可能是只有构建产物的重新打包）。_')
  }

  out.push('---', '')
  out.push(`由 \`.github/scripts/changelog.mjs\` 依据 git 提交记录自动生成。`)

  process.stdout.write(out.join('\n') + '\n')
}

main()
