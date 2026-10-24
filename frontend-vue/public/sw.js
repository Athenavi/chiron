/* Chiron Service Worker — 离线缓存 + 资源缓存 + 缓存策略优化 */
const CACHE_NAME = 'chiron-v1'
const MAX_CACHE_AGE = 7 * 24 * 60 * 60 * 1000 // 7 days

// 需要强制网络的请求路径
const NETWORK_ONLY_PATHS = ['/v1/', '/events', '/ws', '/media/s/', '/submit', '/cancel']

// 静态资源扩展名（使用长效缓存）
const STATIC_EXTENSIONS = ['.js', '.css', '.html', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.woff', '.woff2', '.ttf', '.eot']

self.addEventListener('install', (event) => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => {
      return self.clients.claim()
    })
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  if (request.method !== 'GET') return
  
  const url = new URL(request.url)
  
  // 仅处理同源请求
  if (url.origin !== self.location.origin) return
  
  // 鉴权 API / 事件流：**只走网络，不落缓存**。
  // 此前这里走 networkFirst 会 cache.put 带用户数据的响应，而登出只清 localStorage、
  // 不清 Cache Storage ⇒ 同浏览器换账号或离线回退（fetch 失败）时可读到上一账号的响应体。
  if (NETWORK_ONLY_PATHS.some(path => url.pathname.startsWith(path))) {
    event.respondWith(fetch(request))
    return
  }
  
  // 导航请求：网络优先，离线回退
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/index.html'))
    )
    return
  }
  
  // 静态资源：缓存优先 + 后台更新
  const isStatic = STATIC_EXTENSIONS.some(ext => url.pathname.endsWith(ext))
  if (isStatic) {
    event.respondWith(fetchAndApply(request, 'cacheFirst'))
    return
  }
  
  // 其他资源：网络优先
  event.respondWith(fetchAndApply(request, 'networkFirst'))
})

/**
 * 统一的缓存策略处理
 */
async function fetchAndApply(request, strategy) {
  const cache = await caches.open(CACHE_NAME)
  
  switch (strategy) {
    case 'cacheFirst':
      return cacheFirst(request, cache)
    case 'networkFirst':
      return networkFirst(request, cache)
    case 'staleWhileRevalidate':
      return staleWhileRevalidate(request, cache)
    default:
      return fetch(request)
  }
}

/** 缓存优先策略 - 用于静态资源 */
async function cacheFirst(request, cache) {
  const cached = await cache.match(request)
  
  if (cached) {
    fetch(request).then((response) => {
      if (response && response.ok) {
        cache.put(request, response.clone())
      }
    }).catch(() => {})
    return cached
  }
  
  try {
    const response = await fetch(request)
    if (response && response.ok) {
      cache.put(request, response.clone())
    }
    return response
  } catch {
    return new Response('', { status: 404, statusText: 'Not Found' })
  }
}

/** 网络优先策略 - 用于 API 和导航 */
async function networkFirst(request, cache) {
  try {
    const response = await fetch(request)
    if (response && response.ok) {
      cache.put(request, response.clone())
    }
    return response
  } catch {
    const cached = await cache.match(request)
    if (cached) return cached
    return new Response('', { status: 503, statusText: 'Service Unavailable' })
  }
}

/** 缓存优先但后台更新 */
async function staleWhileRevalidate(request, cache) {
  const cached = await cache.match(request)
  
  const fetchPromise = fetch(request).then((response) => {
    if (response && response.ok) {
      cache.put(request, response.clone())
    }
    return response
  }).catch(() => {
    return cached
  })
  
  return cached || fetchPromise
}
