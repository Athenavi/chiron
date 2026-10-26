/**
 * 主进程与 preload 共享的启动契约。
 *
 * 两边各写一份 `--chiron-api-url=` 前缀时，改一处就会让地址注入静默失效：
 * 主进程换了前缀、preload 还按旧前缀找，页面会静默回落到同源请求。
 */
const API_URL_ARG_PREFIX = '--chiron-api-url='
const DEFAULT_DEV_URL = 'http://localhost:5173'

module.exports = { API_URL_ARG_PREFIX, DEFAULT_DEV_URL }
