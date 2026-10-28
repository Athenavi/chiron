'use strict'
/**
 * 首次启动时**按需下载**运行时原料（uv 二进制）。
 *
 * 安装包不再携带任何 Python 运行时，连 uv 也不带：一切都由这里在首次启动时取。
 * 三个刻意的取舍：
 *
 *   1. **多源测速择优**（`rankSources`）。写死单一源等于把某家服务商的可用性变成
 *      产品前提：实测 github.com 在不少网络下连 TCP 都不通，而官方 PyPI 只有
 *      190KB/s、镜像能到 16MB/s。所以仓库里列候选源，运行期各取一小段计时选最快的。
 *   2. **强校验 SHA256**（来自 `uv_manifest.json`，版本/地址/摘要三者固化）。
 *      多源下载意味着"从哪拿的"不再可预见，唯一能把关的就是内容摘要；不符直接失败，
 *      保留坏文件供排查，绝不"先装上再说"（`vendor/规划.md` §8）。
 *   3. **不自动下载工具**的边界：这里下载的是**产品自身的运行时**，且版本与摘要都在
 *      仓库里固化、可审计；不是"顺手装个构建工具"。
 */
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const zlib = require('node:zlib')

/** 测速只取头部 64KB：够区分"通/不通、快/慢"，又不至于为了选源先下完整个文件 */
const PROBE_BYTES = 64 * 1024
const PROBE_TIMEOUT_MS = 8000
const DOWNLOAD_TIMEOUT_MS = 10 * 60 * 1000

/** 用 Range 取一小段并计时；失败返回 Infinity（排序时自然沉底） */
async function probeSource(url, options = {}) {
  const fetchImpl = options.fetch || globalThis.fetch
  const bytes = options.bytes || PROBE_BYTES
  const timeout = options.timeout || PROBE_TIMEOUT_MS
  if (typeof fetchImpl !== 'function') return { url, bytesPerSec: 0, ok: false }
  const started = Date.now()
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  try {
    const res = await fetchImpl(url, {
      headers: { Range: `bytes=0-${bytes - 1}` },
      signal: controller.signal,
    })
    if (!res.ok || !res.body) return { url, bytesPerSec: 0, ok: false }
    let got = 0
    for await (const chunk of res.body) {
      got += chunk.length
      if (got >= bytes) break
    }
    const elapsed = Math.max(Date.now() - started, 1)
    return { url, bytesPerSec: Math.round((got / elapsed) * 1000), ok: got > 0, got }
  } catch {
    return { url, bytesPerSec: 0, ok: false }
  } finally {
    clearTimeout(timer)
  }
}

/**
 * 候选源测速并排序。**并发探测**：串行会让首次启动白等 N 倍超时。
 * `preferred` 非空时只测它（用户显式指定源，不再挑）。
 */
async function rankSources(urls, options = {}) {
  const list = (urls || []).filter(Boolean)
  if (!list.length) return []
  if (options.preferred) return [{ url: options.preferred, bytesPerSec: Infinity, ok: true, preferred: true }]
  const results = await Promise.all(list.map(url => probeSource(url, options)))
  return results
    .filter(item => item.ok)
    .sort((a, b) => b.bytesPerSec - a.bytesPerSec)
}

/** 下载到文件；`onProgress(loaded, total)` 用于把进度喂给安装窗 */
async function downloadTo(url, dest, options = {}) {
  const fetchImpl = options.fetch || globalThis.fetch
  if (typeof fetchImpl !== 'function') throw new Error('当前环境没有 fetch，无法下载运行时')
  const timeout = options.timeout || DOWNLOAD_TIMEOUT_MS
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  try {
    const res = await fetchImpl(url, { signal: controller.signal })
    if (!res.ok) throw new Error(`下载失败 HTTP ${res.status}：${url}`)
    const total = Number(res.headers.get('content-length') || 0)
    fs.mkdirSync(path.dirname(dest), { recursive: true })
    const staging = `${dest}.part`
    const out = fs.createWriteStream(staging)
    let loaded = 0
    try {
      for await (const chunk of res.body) {
        loaded += chunk.length
        if (!out.write(chunk)) await new Promise(r => out.once('drain', r))
        if (typeof options.onProgress === 'function') options.onProgress(loaded, total)
      }
    } finally {
      await new Promise((resolve, reject) => out.end(err => (err ? reject(err) : resolve())))
    }
    fs.renameSync(staging, dest)
    return { path: dest, bytes: loaded }
  } finally {
    clearTimeout(timer)
  }
}

function sha256File(file) {
  const hash = crypto.createHash('sha256')
  hash.update(fs.readFileSync(file))
  return hash.digest('hex')
}

/**
 * 从 ZIP（uv 的 PyPI wheel）里取出指定文件。**不依赖外部工具**：Windows 的 tar、
 * 各平台的 unzip 都不保证存在，而"找不到解压工具"会让首次启动直接失败。
 *
 * 只支持 stored(0) 与 deflate(8) —— wheel 里就这两种，够用且不必引依赖。
 */
function extractFromZip(zipPath, matcher, destPath) {
  const buf = fs.readFileSync(zipPath)
  // 从尾部往前找 EOCD（0x06054b50），它后面还可能跟 ZIP 注释
  let eocd = -1
  for (let i = buf.length - 22; i >= 0 && i >= buf.length - 65558; i -= 1) {
    if (buf.readUInt32LE(i) === 0x06054b50) {
      eocd = i
      break
    }
  }
  if (eocd < 0) throw new Error(`不是有效的 zip（找不到目录区）：${zipPath}`)
  const count = buf.readUInt16LE(eocd + 10)
  let offset = buf.readUInt32LE(eocd + 16)

  for (let index = 0; index < count; index += 1) {
    if (buf.readUInt32LE(offset) !== 0x02014b50) break
    const method = buf.readUInt16LE(offset + 10)
    const compSize = buf.readUInt32LE(offset + 20)
    const nameLen = buf.readUInt16LE(offset + 28)
    const extraLen = buf.readUInt16LE(offset + 30)
    const commentLen = buf.readUInt16LE(offset + 32)
    const localOffset = buf.readUInt32LE(offset + 42)
    const name = buf.toString('utf8', offset + 46, offset + 46 + nameLen)
    offset += 46 + nameLen + extraLen + commentLen
    if (!matcher(name)) continue

    // 数据起点要从 local header 重算：它的 extra 长度可能与目录区不同
    if (buf.readUInt32LE(localOffset) !== 0x04034b50) throw new Error(`zip 条目损坏：${name}`)
    const lNameLen = buf.readUInt16LE(localOffset + 26)
    const lExtraLen = buf.readUInt16LE(localOffset + 28)
    const start = localOffset + 30 + lNameLen + lExtraLen
    const raw = buf.subarray(start, start + compSize)
    const body = method === 0 ? raw : zlib.inflateRawSync(raw)
    fs.mkdirSync(path.dirname(destPath), { recursive: true })
    fs.writeFileSync(destPath, body)
    if (process.platform !== 'win32') fs.chmodSync(destPath, 0o755)
    return { entry: name, bytes: body.length }
  }
  throw new Error(`zip 内找不到所需文件：${zipPath}`)
}

module.exports = {
  PROBE_BYTES,
  probeSource,
  rankSources,
  downloadTo,
  sha256File,
  extractFromZip,
}
