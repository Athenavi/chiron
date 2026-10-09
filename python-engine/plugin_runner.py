"""插件沙箱 runner — 独立子进程入口（勿 import 任何 app 内模块！）。

由 app/api/plugins.py 的 run_plugin_in_sandbox 以
``sys.executable plugin_runner.py`` 方式拉起，payload 走 stdin（避免
命令行长度限制与注入），结果 JSON 走 stdout。

隔离层次：
- B 层（进程级）：独立 subprocess，崩溃/超时/炸弹不影响宿主；
  POSIX 下 setrlimit 限内存(RSS/AS)与 CPU 时间；Windows 无 resource
  模块，依赖父进程 timeout + kill。
- A 层（代码级）：复用 app/tools/code_guard.py 的静态 AST 检查 +
  受控 builtins（按文件路径加载，避开 app.tools.__init__ 的重量级
  import 链 — 否则子进程会拉起整个 FastAPI 应用）。

契约：任何失败都以 ``{"success": false, "error": ...}`` JSON 输出且
exit 0（父进程按内容判定）；仅当 runner 自身崩溃（无法输出 JSON）才
非零退出，父进程按 fail-loud 处理。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_code_guard():
    """按文件路径加载守卫模块（不触发 app.tools.__init__）。"""
    guard_path = _HERE / "app" / "tools" / "code_guard.py"
    spec = importlib.util.spec_from_file_location("code_guard", guard_path)
    if spec is None or spec.loader is None:  # pragma: no cover — 文件必然存在
        raise RuntimeError(f"cannot load code_guard from {guard_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply_resource_limits(max_memory_mb: int, max_cpu_seconds: float) -> None:
    """POSIX 下施加 rlimit；Windows 静默跳过（由父进程 timeout 兜底）。"""
    try:
        import resource
    except ImportError:
        return  # Windows — 无 resource 模块
    try:
        mem_bytes = max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        cpu = (int(max_cpu_seconds), int(max_cpu_seconds) + 1)
        resource.setrlimit(resource.RLIMIT_CPU, cpu)
    except (ValueError, OSError):
        # rlimit 超过系统硬限等场景：记录到 stderr，不阻断启动（父进程仍限时）
        print("plugin_runner: setrlimit failed, falling back to parent timeout", file=sys.stderr)


def _run_plugin(guard, payload: dict) -> dict:
    plugin_name = payload.get("plugin_name", "<unknown>")
    code = payload.get("code", "")
    input_data = payload.get("input", {})

    if not code.strip():
        return {"success": False, "error": "plugin code is empty"}

    # A 层：静态 AST 检查
    denied = guard.check_static(code)
    if denied:
        return {"success": False, "error": f"plugin blocked by static guard: {denied}"}

    # A 层：受控 builtins
    safe_globals: dict = {
        "__builtins__": guard.safe_builtins(),
        "__name__": f"plugin_{plugin_name}",
        "__file__": f"/plugins/{plugin_name}/main.py",
    }
    local_ns: dict = {}

    exec(compile(code, f"<plugin:{plugin_name}>", "exec"), safe_globals, local_ns)  # noqa: S102 — 沙箱语义由 guard 约束

    main_fn = local_ns.get("main")
    if not callable(main_fn):
        return {"success": False, "error": "Plugin must export 'main(input)' function"}

    output = main_fn(input_data)
    return {"success": True, "output": output}


def _write_stdout(text: str) -> None:
    """把结果按 **UTF-8** 写 stdout。

    为什么不用 `print`：它走的是**进程 locale 编码**（Windows 上通常是 cp936），而宿主
    （`app/hooks/runner.py` / `app/api/plugins.py`）按 **UTF-8** 解码 —— 内容含非 ASCII 时
    会变成乱码或直接失败（实测：hook 源码含中文 ⇒ 宿主拿到的 code 已损坏 ⇒ `ast.parse` 报
    非 UTF-8 语法错误）。两端同源（UTF-8）才算契约。
    """
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        # 同款对称：宿主发的是 **UTF-8 字节**，这里必须显式按 UTF-8 解码，
        # 不能依赖 `sys.stdin.read()` 的 locale 编码。
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        _write_stdout(json.dumps({"success": False, "error": f"invalid runner payload: {e}"}))
        return 0

    try:
        _apply_resource_limits(
            max_memory_mb=int(payload.get("max_memory_mb", 256)),
            max_cpu_seconds=float(payload.get("max_cpu_seconds", 10.0)),
        )
    except (TypeError, ValueError):
        pass  # 配置非法时用默认值，不阻断

    guard = _load_code_guard()
    try:
        result = _run_plugin(guard, payload)
    except RuntimeError as e:
        result = {"success": False, "error": str(e)}
    except Exception:  # noqa: BLE001 — 插件异常结构化返回，不崩溃 runner
        result = {"success": False, "error": traceback.format_exc(limit=5)}

    # 输出必须可 JSON 序列化；不可序列化的 output 降级为字符串
    try:
        json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        result = {
            "success": result.get("success", False),
            "output": str(result.get("output")) if "output" in result else None,
            "error": result.get("error") or "plugin output is not JSON-serializable (stringified)",
        }
    _write_stdout(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
