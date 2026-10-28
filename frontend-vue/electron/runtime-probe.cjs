'use strict'
/**
 * "这个解释器能不能跑起本地 core" 的唯一判据。
 *
 * **为什么单独一个文件**：判断这件事的地方有两处 ——
 *   1. `components.cjs::probeBundledRuntime`（启动前挑解释器，含回退链）；
 *   2. `runtime-install.cjs`（首次安装后验收 venv 是否真的装好了）。
 * 两处各写一份探测代码，就会出现"安装脚本认为装好了、启动时却 import 失败"这种
 * 自相矛盾的状态 —— 而这正是上一轮 `httpx` 事故的形态：自检用**手维护的模块清单**，
 * 清单漏了 `httpx`，于是自检绿、运行时红，用户看到所有页面"服务繁忙"。
 *
 * 因此探测代码只在这里写一次，两边共用。
 *
 * **为什么不用模块清单**：`pkgutil.walk_packages` + 真实 `import` 让 Python 自己算
 * 依赖闭包。清单只覆盖作者想到的东西，闭包覆盖全部。
 *
 * **`walk_packages` 的坑**：它的 `onerror` 默认是 `None`，会**静默吞掉**子包的 import
 * 失败，那棵失败的子树连同子孙都不会出现在遍历结果里 —— 探测会假通过。必须显式
 * 收集错误。
 */

/**
 * 在 core 的源码目录里真实导入整个 `app` 包。
 *
 * 逐模块 import 而不是只 `import app`：`app/__init__.py` 是空的，仅导入它等于什么都没验。
 *
 * 输出约定（供调用方解析，不要改动格式）：
 *   - `MISS <name>: <异常>` 每个失败一行；
 *   - 全部成功时退出码 0。
 */
const RUNTIME_PROBE_CODE = [
  'import importlib, pkgutil, sys',
  'missing = []',
  'try:',
  '    import app',
  'except Exception as e:',
  '    print("MISS app: %s: %s" % (type(e).__name__, e))',
  '    sys.exit(1)',
  'def _onerror(name):',
  '    print("MISS %s: %s" % (name, sys.exc_info()[1]))',
  '    missing.append(name)',
  'mods = list(pkgutil.walk_packages(app.__path__, "app.", onerror=_onerror))',
  'for m in mods:',
  '    try:',
  '        importlib.import_module(m.name)',
  '    except Exception as e:',
  '        print("MISS %s: %s: %s" % (m.name, type(e).__name__, e))',
  '        missing.append(m.name)',
  'sys.exit(1 if missing else 0)',
].join('\n')

/** 从探测输出里认出"缺哪个模块"；认不出返回空串（不猜） */
function extractMissingModule(output) {
  const match = String(output || '').match(/No module named ['"]?([A-Za-z_][\w.]*)/)
  return match ? match[1] : ''
}

/**
 * 跑一次探测。返回统一结构，调用方不需要知道 spawnSync 的细节。
 *
 * @param {Function} spawnSync 注入的 child_process.spawnSync（便于单测替身）
 * @param {string} pythonPath 解释器绝对路径
 * @param {string} cwd `app` 包所在目录（core 将来启动时的 cwd）
 * @param {object} [options] `{ timeout, env }`
 */
function probeRuntime(spawnSync, pythonPath, cwd, options = {}) {
  if (!pythonPath) {
    return { ok: false, reason: 'missing', message: '未提供解释器路径' }
  }
  if (typeof spawnSync !== 'function') {
    // 拿不到 spawn 能力就不要假装通过：让后续真实启动去暴露问题
    return { ok: true, reason: 'unverified', message: '' }
  }

  let result
  try {
    result = spawnSync(pythonPath, ['-c', RUNTIME_PROBE_CODE], {
      timeout: options.timeout || 30000,
      windowsHide: true,
      encoding: 'utf8',
      // cwd 必须是 core 将来启动时的那个目录，否则 `import app` 找的不是这份源码
      ...(cwd ? { cwd } : {}),
      // PYTHONNOUSERSITE：不让用户级 site-packages 混进来，否则"用户机恰好装过 fastapi"
      // 会让探测通过、而换台机器就崩 —— 必须只验本 venv 自带的依赖。
      env: { ...process.env, PYTHONNOUSERSITE: '1', ...(options.env || {}) },
    })
  } catch (err) {
    return {
      ok: false,
      reason: 'probe-failed',
      message: err && err.message ? err.message : String(err),
    }
  }

  if (result && result.status === 0) {
    return { ok: true, reason: 'ok', message: '' }
  }

  const output = String((result && (result.stderr || '')) + (result && (result.stdout || '')) || '')
  const missing = extractMissingModule(output)
  const lastLine = output.trim().split('\n').pop() || '未知原因'
  return {
    ok: false,
    reason: missing ? 'missing-dependency' : 'probe-failed',
    missingModule: missing,
    message: missing
      ? `解释器 ${pythonPath} 缺少模块 ${missing}`
      : `解释器 ${pythonPath} 无法导入本地核心：${lastLine}`,
  }
}

module.exports = {
  RUNTIME_PROBE_CODE,
  extractMissingModule,
  probeRuntime,
}
