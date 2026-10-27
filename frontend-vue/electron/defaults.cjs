'use strict'
/**
 * 本地服务端点的**集中默认值**（只放常量与名字，不放启动逻辑）。
 *
 * 为什么单独一个文件：端口、host、重试次数、超时、组件名这些值原来散在
 * components.cjs / main.cjs / inference-proxy.cjs 里各写一份（`127.0.0.1`、
 * `/healthz`、`30000`…），改一处就会和别人对不上。这里只做"唯一来源"，
 * 其它模块（含单测）一律 require 本文件，不再自己写字面量。
 *
 * 硬规则：**不写死任何固定端口**。
 *   - `DEFAULT_CORE_PORT = 0` 表示"交给内核随机分配"，这是唯一安全的默认值；
 *     端口冲突时换端口重试（见 components.cjs::startCore）而不是改字面量。
 *   - 需要指定端口时由调用方显式传，不要在这里写具体数字（例如 Ollama 的默认端口）。
 */
/** 只绑 loopback（契约 §4）：本地服务不对局域网暴露 */
const DEFAULT_HOST = '127.0.0.1'
/** 0 = 内核分配。只有内核知道最终端口，靠 stdout 的 listening 事件回读 */
const DEFAULT_CORE_PORT = 0
/** 就绪探测路径（core 的 GET /healthz） */
const DEFAULT_HEALTH_PATH = '/healthz'

/** 契约 §2 的组件名，顺序即 UI 展示顺序（core 由壳层自己起，其余委托 core） */
const COMPONENT_NAMES = Object.freeze(['core', 'pageindex', 'langextract'])

/** core 启动尝试次数（含首次）：端口冲突 / 进程提前退出 / 健康检查超时都会换端口重试 */
const CORE_PORT_ATTEMPTS = 3
/** 单次尝试等 core 就绪的上限 */
const CORE_HEALTH_TIMEOUT_MS = 30000
/** 就绪轮询间隔 */
const CORE_HEALTH_INTERVAL_MS = 200
/** 单次健康探测的请求超时（要远小于上面的总超时） */
const CORE_PROBE_TIMEOUT_MS = 1000
/** SIGTERM 之后等多久再 SIGKILL（给 uvicorn 关 PG 留时间） */
const CORE_STOP_TIMEOUT_MS = 5000
/** SIGKILL 之后最多再等多久（进程树终止也用它做上限） */
const CORE_KILL_TIMEOUT_MS = 2000
/** 报错消息里最多带多少字节 stderr（脱敏后）：够定位 ImportError / 端口占用即可 */
const CORE_STDERR_TAIL_BYTES = 2000

/** core 的组件端点（desktop-local/app/api/components.py） */
const CORE_COMPONENTS_PATH = '/v1/components'
/** 列组件应该很快：卡住就别让 UI 转圈 */
const CORE_LIST_TIMEOUT_MS = 5000
/** 安装要复制/解压组件包，给宽一点 */
const CORE_INSTALL_TIMEOUT_MS = 120000
/** 启动可能要等健康检查（core 侧默认 20s），再留点余量 */
const CORE_ACTION_TIMEOUT_MS = 60000

/**
 * 端口/地址被占用的特征串（大小写不敏感）。
 *
 * 用途只有两个：把"这次失败属于端口冲突"写进可读错误、并在重试前说明原因。
 * 判断"能否重试"不依赖这张表 —— 除了 ENOENT/EACCES 这类不可恢复的 spawn 错误，
 * 其它失败一律换端口重试（端口占用在内核里可能以任何措辞出现）。
 * Python 侧 desktop-local/app/{storage/pg.py,components/manager.py} 用同一组特征串。
 */
const PORT_CONFLICT_PATTERNS = Object.freeze([
  'address already in use',
  'address in use',
  'eaddrinuse',
  'only one usage of each socket address',
  'port is already allocated',
  'could not bind',
  'failed to bind',
  'cannot bind',
  'bind failed',
  'another server might be running',
  'port already in use',
])

/** 文本里是否像"端口被占用"。空文本一律 false（不猜）。 */
function isPortConflict(text) {
  const value = String(text || '').toLowerCase()
  if (!value) return false
  return PORT_CONFLICT_PATTERNS.some(pattern => value.includes(pattern))
}

module.exports = {
  DEFAULT_HOST,
  DEFAULT_CORE_PORT,
  DEFAULT_HEALTH_PATH,
  COMPONENT_NAMES,
  CORE_PORT_ATTEMPTS,
  CORE_HEALTH_TIMEOUT_MS,
  CORE_HEALTH_INTERVAL_MS,
  CORE_PROBE_TIMEOUT_MS,
  CORE_STOP_TIMEOUT_MS,
  CORE_KILL_TIMEOUT_MS,
  CORE_STDERR_TAIL_BYTES,
  CORE_COMPONENTS_PATH,
  CORE_LIST_TIMEOUT_MS,
  CORE_INSTALL_TIMEOUT_MS,
  CORE_ACTION_TIMEOUT_MS,
  PORT_CONFLICT_PATTERNS,
  isPortConflict,
}
