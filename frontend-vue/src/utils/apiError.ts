/**
 * 把 HTTP/网络错误翻译成用户能看懂的话（i18n 版）。
 *
 * 优先级：后端稳定错误码（`code`）的本地化文案 → 已知状态码文案 → 后端 `error`/
 * `message` 原文 → 调用方兜底文案。
 *
 * 为什么 code 优先：后端 `error` 是面向集成方的原文（英文/中文混排），不随界面语言
 * 变化；而 `code` 是稳定契约（见 internal/api/error_codes.go），前端按当前语言渲染
 * （src/locales/<lang>/errors.ts），因此切语言时错误提示同步切换。
 *
 * 服务端 5xx 例外：仍把后端原文附在括号里 —— 那是让用户与运维定位问题的唯一线索。
 */
import { t, hasMessage } from '../i18n'

interface ApiErrorLike {
  response?: {
    status?: number
    data?: { error?: string; message?: string; code?: string }
  }
  code?: string
  message?: string
}

/** HTTP 状态码 → errors.* 键；未收录返回 null（交给后端文案或兜底） */
export function statusMessageKey(status?: number): string | null {
  switch (status) {
    case 400: return 'invalid_request'
    case 401: return 'auth_required'
    case 402: return 'insufficient_credits'
    case 403: return 'forbidden'
    case 404: return 'not_found'
    case 408: return 'timeout'
    case 413: return 'payload_too_large'
    case 429: return 'rate_limited'
    case 500:
    case 502:
    case 503:
    case 504: return 'service_unavailable'
    default: return null
  }
}

/** 已知状态码的本地化说明；未收录返回 null（交给后端文案或兜底） */
export function statusMessage(status?: number): string | null {
  const key = statusMessageKey(status)
  return key ? t(`errors.${key}`) : null
}

/** 5xx 是服务端问题：只给通用文案会让用户和运维都无从下手 */
const SERVER_ERROR_MIN = 500

interface ErrorResponseLike {
  response?: { status?: number; data?: { error?: unknown } }
}

/**
 * 取后端 `error` 原文；取不到（网络错误 / 非 axios 异常 / 后端没给）时用调用方兜底。
 *
 * 与 `describeApiError` 的分工：**这里只取原文，不做本地化替换**。
 * 有些调用点要按原文或状态码分支（例如读 `current_password is required` 来判断
 * 「该账号已设置密码」），换成通用文案会把分支依据弄丢。
 * 文案是给用户看、且不需要分支时，用 `describeApiError` 才是本地化版本。
 */
export function serverErrorMessage(error: unknown, fallback: string): string {
  const detail = (error as ErrorResponseLike).response?.data?.error
  return typeof detail === 'string' && detail ? detail : fallback
}

/** 取 HTTP 状态码（用于 429/409 这类要单独提示的分支） */
export function errorStatus(error: unknown): number | undefined {
  return (error as ErrorResponseLike).response?.status
}

/** 后端错误码的本地化文案；无对应文案时返回 null（回退到 error 原文） */
function localizedCodeMessage(code?: string): string | null {
  if (!code) return null
  const key = `errors.${code}`
  return hasMessage(key) ? t(key) : null
}

export function describeApiError(error: unknown, fallback?: string): string {
  const err = (error ?? {}) as ApiErrorLike
  const status = err.response?.status
  const serverMessage = err.response?.data?.error || err.response?.data?.message
  const code = err.response?.data?.code

  const byCode = localizedCodeMessage(code)
  const known = statusMessage(status)

  // 服务端错误把后端原文一并带出：那是定位问题的唯一线索
  // （例如 500 背后的 "failed to create payment order" / "支付下单失败"）
  if (status && status >= SERVER_ERROR_MIN) {
    const base = byCode ?? known
    if (base && serverMessage && serverMessage !== base) return `${base}（${serverMessage}）`
    return base ?? serverMessage ?? t('errors.internal_error')
  }

  if (byCode) return byCode
  if (known) return known
  if (serverMessage) return serverMessage
  if (status) return t('errors.http_status', { status })

  const message = err.message || ''
  if (err.code === 'ECONNABORTED' || /timeout/i.test(message)) return t('errors.timeout')
  if (/network\s*error/i.test(message)) return t('errors.network_error')
  return fallback ?? t('errors.request_failed')
}
