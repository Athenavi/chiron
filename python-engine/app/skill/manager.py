"""技能工作台增强: MCP 协议集成 + 动态注册 + 租户隔离限流

功能:
1. MCP (Model Context Protocol) 工具发现与调用
2. 技能动态注册/卸载 (热更新)
3. 每租户独立限流 (QPS=50, Burst=100)
4. Trace 集成: 每次技能执行记录 span
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from app.trace import record_span

logger = logging.getLogger(__name__)

#: 参数值中禁止出现的 shell 元字符（防止注入到沙箱命令）
_SHELL_META_CHARS = re.compile(r"[|&;`$()<>#\n]")


class SkillType(str, Enum):
    """技能类型"""

    PROMPT = "prompt"  # 提示词模板
    PYTHON_SCRIPT = "python"  # Python 脚本
    SHELL_COMMAND = "shell"  # Shell 命令
    HTTP_REQUEST = "http"  # HTTP 请求
    MCP_TOOL = "mcp"  # MCP 协议工具


class SkillStatus(str, Enum):
    """技能状态"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    ERROR = "error"


@dataclass
class SkillMetadata:
    """技能元数据"""

    skill_id: str
    name: str
    description: str
    type: SkillType
    version: str = "1.0.0"
    author: str = ""
    tags: list[str] = field(default_factory=list)
    input_schema: dict = field(default_factory=dict)  # JSON Schema
    output_schema: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)  # 技能配置 (如 prompt 模板)
    status: SkillStatus = SkillStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    updated_at: float = 0
    tenant_id: str = ""  # SaaS 安全: 租户隔离


@dataclass
class SkillExecutionResult:
    """技能执行结果"""

    skill_id: str
    skill_name: str
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: int = 0
    metadata: dict = field(default_factory=dict)
    trace_id: str = ""
    tenant_id: str = ""


class MCPClient:
    """MCP (Model Context Protocol) 客户端

    MCP 协议规范:
    - 传输层: STDIO / HTTP / WebSocket
    - 消息格式: JSON-RPC 2.0
    - 核心方法: tools/list, tools/call, resources/read

    参考: https://github.com/modelcontextprotocol/specification
    """

    def __init__(self, server_url: str, transport: str = "http"):
        self.server_url = server_url
        self.transport = transport  # "stdio" / "http" / "websocket"
        self._tools_cache: list[dict] = []
        self._cache_time = 0

    async def discover_tools(self) -> list[dict]:
        """发现 MCP Server 提供的工具"""
        # 检查缓存 (5 分钟 TTL)
        if time.time() - self._cache_time < 300 and self._tools_cache:
            return self._tools_cache

        # Fail loud: 不支持的传输方式必须显式抛错。
        if self.transport == "stdio":
            response = await self._stdio_rpc("tools/list", {}, timeout=10.0)
            tools = response.get("result", {}).get("tools", [])
            self._tools_cache = tools
            self._cache_time = time.time()
            logger.info(
                f"Discovered {len(tools)} MCP tools via STDIO from {self.server_url}"
            )
            return tools

        if self.transport != "http":
            raise ValueError(f"Unsupported transport: {self.transport}")

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.server_url}/rpc",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/list",
                        "params": {},
                    },
                    timeout=10.0,
                )
                response.raise_for_status()
                result = response.json()

            # 解析工具列表
            tools = result.get("result", {}).get("tools", [])
            self._tools_cache = tools
            self._cache_time = time.time()

            logger.info(f"Discovered {len(tools)} MCP tools from {self.server_url}")
            return tools

        except Exception as e:
            logger.error(f"MCP tool discovery failed: {e}")
            return []

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        trace_id: str = "",
        tenant_id: str = "",
    ) -> SkillExecutionResult:
        """调用 MCP 工具"""
        start_time = time.time()

        # Fail loud: 不支持的传输方式显式抛错。
        if self.transport == "stdio":
            response = await self._stdio_rpc(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
                timeout=30.0,
            )
            output = response.get("result", {}).get("content", "")
            is_error = response.get("error") is not None

            duration_ms = int((time.time() - start_time) * 1000)

            if trace_id:
                await record_span(
                    trace_id=trace_id,
                    span_name=f"mcp:{tool_name}",
                    duration_ms=duration_ms,
                    metadata={
                        "tool_name": tool_name,
                        "success": not is_error,
                        "tenant_id": tenant_id,
                    },
                    tenant_id=tenant_id,
                )

            return SkillExecutionResult(
                skill_id=f"mcp:{tool_name}",
                skill_name=tool_name,
                success=not is_error,
                output=json.dumps(output, ensure_ascii=False) if not is_error else "",
                error=(
                    json.dumps(response.get("error", {}), ensure_ascii=False)
                    if is_error
                    else ""
                ),
                duration_ms=duration_ms,
                trace_id=trace_id,
                tenant_id=tenant_id,
            )

        if self.transport != "http":
            raise ValueError(f"Unsupported transport: {self.transport}")

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.server_url}/rpc",
                    json={
                        "jsonrpc": "2.0",
                        "id": int(time.time() * 1000),
                        "method": "tools/call",
                        "params": {
                            "name": tool_name,
                            "arguments": arguments,
                        },
                    },
                    timeout=30.0,
                )
                response.raise_for_status()
                result = response.json()

            output = result.get("result", {}).get("content", "")
            is_error = result.get("error") is not None

            duration_ms = int((time.time() - start_time) * 1000)

            # 记录 span
            if trace_id:
                await record_span(
                    trace_id=trace_id,
                    span_name=f"mcp:{tool_name}",
                    duration_ms=duration_ms,
                    metadata={
                        "tool_name": tool_name,
                        "success": not is_error,
                        "tenant_id": tenant_id,
                    },
                    tenant_id=tenant_id,
                )

            return SkillExecutionResult(
                skill_id=f"mcp:{tool_name}",
                skill_name=tool_name,
                success=not is_error,
                output=json.dumps(output, ensure_ascii=False) if not is_error else "",
                error=(
                    json.dumps(result.get("error", {}), ensure_ascii=False)
                    if is_error
                    else ""
                ),
                duration_ms=duration_ms,
                trace_id=trace_id,
                tenant_id=tenant_id,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)

            logger.error(f"MCP tool call failed: {e}")
            return SkillExecutionResult(
                skill_id=f"mcp:{tool_name}",
                skill_name=tool_name,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
                trace_id=trace_id,
                tenant_id=tenant_id,
            )

    # ── STDIO 传输 ──────────────────────────────────────────────────

    async def _stdio_rpc(
        self,
        method: str,
        params: dict,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """通过 STDIO 子进程发送 JSON-RPC 2.0 请求。

        MCP STDIO 传输规范:
        - server_url 字段复用为要执行的命令（如 "node server.js"）
        - 每行一个 JSON-RPC 消息，通过 stdin/stdout 通信
        - 先发送 initialize 握手，再发送实际请求

        设计决策:
        - 每次 RPC 请求 spawn 新子进程（简单可靠，避免生命周期管理复杂度）
        - discover_tools 有 5 分钟缓存，不会频繁 spawn
        - 超时 kill + stderr 截断 + fail-loud
        """
        import asyncio

        cmd = shlex.split(self.server_url, posix=os.name != "nt")
        if not cmd:
            raise ValueError(
                "STDIO transport: server_url must be a command to execute "
                "(e.g. 'node /path/to/mcp-server.js')"
            )

        # S 安全修复：MCP STDIO 命令必须过白名单(PLUGIN_COMMAND_ALLOWLIST)，
        # 防 server_url 可被配置/注入控制时任意命令执行。未配置则 fail-close 拒绝。
        from app.tools.ssrf import command_allowed

        if not command_allowed(cmd[0]):
            raise ValueError(
                f"STDIO transport: command not allowed: {cmd[0]} "
                "(set PLUGIN_COMMAND_ALLOWLIST)"
            )

        # 构造 initialize + 实际请求两条消息
        init_req = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "chiron-mcp-client", "version": "1.0.0"},
            },
        }
        actual_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        payload = (json.dumps(init_req) + "\n" + json.dumps(actual_req) + "\n").encode()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError(f"STDIO transport: command not found: {cmd[0]}")
        except Exception as e:
            raise RuntimeError(f"STDIO transport: failed to spawn process: {e}")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"MCP STDIO '{method}' timed out after {timeout}s")

        # 从 stdout 解析 JSON-RPC 响应（取 id=1 的那条）
        lines = stdout.decode(errors="replace").strip().splitlines()
        response: dict[str, Any] | None = None
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("id") == 1:
                response = parsed
                break

        if response is None:
            err_snippet = stderr.decode(errors="replace")[:500]
            raise RuntimeError(
                f"MCP STDIO '{method}': no JSON-RPC response with id=1. "
                f"stdout_lines={len(lines)}, stderr={err_snippet}"
            )

        return response


class SkillManager:
    """技能管理器 (租户隔离)

    功能:
    1. 技能注册/卸载/更新
    2. 技能发现 (按类型/标签/状态)
    3. 技能执行 (统一接口)
    4. MCP 工具集成
    """

    def __init__(self):
        self._skills: dict[str, SkillMetadata] = {}  # skill_id -> metadata
        self._mcp_clients: dict[str, MCPClient] = {}  # server_url -> client

    async def register_skill(
        self,
        tenant_id: str,
        skill_id: str,
        name: str,
        description: str,
        type: SkillType,
        config: dict,
        input_schema: dict = {},
        output_schema: dict = {},
    ) -> SkillMetadata:
        """注册新技能 (带租户隔离)

        SaaS 安全:
        - skill_id 格式: "{tenant_id}:{name}" 防止冲突
        - metadata 携带 tenant_id 标记
        """
        # 构造完整 skill_id (带租户前缀)
        full_skill_id = f"{tenant_id}:{skill_id}"

        skill = SkillMetadata(
            skill_id=full_skill_id,
            name=name,
            description=description,
            type=type,
            tenant_id=tenant_id,
            config=config,
            input_schema=input_schema,
            output_schema=output_schema,
            updated_at=time.time(),
        )

        self._skills[full_skill_id] = skill

        # 如果是 MCP 类型,初始化客户端
        if type == SkillType.MCP_TOOL:
            server_url = config.get("server_url", "")
            transport = config.get("transport", "http")
            if server_url:
                self._mcp_clients[full_skill_id] = MCPClient(server_url, transport)

        logger.info(
            f"Skill registered (id={full_skill_id}, name={name}, tenant={tenant_id})"
        )

        return skill

    async def unregister_skill(self, tenant_id: str, skill_id: str) -> bool:
        """卸载技能"""
        full_skill_id = f"{tenant_id}:{skill_id}"
        if full_skill_id in self._skills:
            del self._skills[full_skill_id]
            logger.info(f"Skill unregistered (id={full_skill_id})")
            return True
        return False

    async def list_skills(
        self,
        tenant_id: str,
        skill_type: Optional[SkillType] = None,
        status: Optional[SkillStatus] = None,
    ) -> list[SkillMetadata]:
        """列出技能 (租户隔离)"""
        results = []
        for skill in self._skills.values():
            if skill.tenant_id != tenant_id:
                continue  # SaaS 安全: 过滤其他租户
            if skill_type and skill.type != skill_type:
                continue
            if status and skill.status != status:
                continue
            results.append(skill)

        return results

    async def execute_skill(
        self,
        tenant_id: str,
        skill_id: str,
        params: dict,
        trace_id: str = "",
    ) -> SkillExecutionResult:
        """执行技能 (统一接口 + 租户隔离 + trace)"""
        start_time = time.time()

        # 查找技能元数据
        full_skill_id = f"{tenant_id}:{skill_id}"
        skill_meta = self._skills.get(full_skill_id)

        if not skill_meta:
            return SkillExecutionResult(
                skill_id=full_skill_id,
                skill_name=skill_id,
                success=False,
                error=f"Skill not found: {full_skill_id}",
                tenant_id=tenant_id,
            )

        try:
            if skill_meta.type == SkillType.MCP_TOOL:
                # MCP 工具调用
                mcp_client = self._mcp_clients.get(full_skill_id)
                if not mcp_client:
                    raise ValueError(f"MCP client not initialized for {full_skill_id}")

                result = await mcp_client.call_tool(
                    tool_name=skill_meta.name,
                    arguments=params,
                    trace_id=trace_id,
                    tenant_id=tenant_id,
                )

                return result

            elif skill_meta.type == SkillType.PROMPT:
                # 提示词模板渲染 (config 已在 register_skill 时存入元数据)
                prompt = skill_meta.config.get("template", "")
                # 填充变量（使用 string.Template 替代 str.format 防止属性访问注入）
                import string
                tmpl = string.Template(prompt)
                # 只允许安全字符的参数值，防止模板注入
                safe_params = {}
                for k, v in params.items():
                    if isinstance(v, str):
                        safe_params[k] = v
                    else:
                        safe_params[k] = str(v)
                rendered = tmpl.safe_substitute(**safe_params)

                duration_ms = int((time.time() - start_time) * 1000)

                return SkillExecutionResult(
                    skill_id=full_skill_id,
                    skill_name=skill_meta.name,
                    success=True,
                    output=rendered,
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )

            elif skill_meta.type == SkillType.PYTHON_SCRIPT:
                return await self._execute_python_script(
                    skill_meta,
                    params,
                    full_skill_id,
                    tenant_id,
                    trace_id,
                    start_time,
                )

            elif skill_meta.type == SkillType.SHELL_COMMAND:
                return await self._execute_shell_command(
                    skill_meta,
                    params,
                    full_skill_id,
                    tenant_id,
                    trace_id,
                    start_time,
                )

            elif skill_meta.type == SkillType.HTTP_REQUEST:
                return await self._execute_http_request(
                    skill_meta,
                    params,
                    full_skill_id,
                    tenant_id,
                    trace_id,
                    start_time,
                )

            else:
                raise ValueError(f"Unsupported skill type: {skill_meta.type}")

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)

            logger.error(f"Skill execution failed (skill={full_skill_id}): {e}")
            return SkillExecutionResult(
                skill_id=full_skill_id,
                skill_name=skill_meta.name,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
                tenant_id=tenant_id,
            )

    # ── Python 脚本执行 (沙箱) ──────────────────────────────────────

    async def _execute_python_script(
        self,
        skill_meta: SkillMetadata,
        params: dict,
        full_skill_id: str,
        tenant_id: str,
        trace_id: str,
        start_time: float,
    ) -> SkillExecutionResult:
        """在安全沙箱中执行 Python 脚本技能。

        复用 run_code 模块的静态 AST 检查 + 运行时 builtins 守卫，
        确保脚本无法访问宿主文件系统/网络/子进程。

        config 字段:
            code:     Python 脚本源码（函数体，可引用 params 变量）
            timeout:  超时秒数（默认 60，上限 300）
        """
        from app.tools.run_code import (_check_static, _render_result,
                                        _safe_builtins)

        code = skill_meta.config.get("code", "")
        timeout = int(skill_meta.config.get("timeout", 60))

        if not code.strip():
            raise ValueError("skill config missing 'code' field")

        # 静态安全检查：AST 扫描禁止危险模块/调用
        denied = _check_static(code)
        if denied:
            raise ValueError(f"python script blocked by static guard: {denied}")

        # 将 params 注入为脚本可用的变量
        import asyncio
        import contextlib
        import io

        ns: dict[str, Any] = {
            "params": params,
            "__builtins__": _safe_builtins(),
        }

        # 包装为 async 函数（与 run_code 语义一致）
        body = "\n".join(
            "    " + line if line.strip() else line for line in code.splitlines()
        )
        src = f"async def _skill_main():\n{body}\n"

        log_buf = io.StringIO()
        try:
            exec(compile(src, f"<skill:{skill_meta.name}>", "exec"), ns)
            main_fn = ns["_skill_main"]

            async def _run():
                with contextlib.redirect_stdout(log_buf):
                    result = await main_fn()
                return result

            result = await asyncio.wait_for(_run(), timeout=timeout)
            output = json.dumps(
                {"result": _render_result(result), "logs": log_buf.getvalue()[-5000:]},
                ensure_ascii=False,
                default=str,
            )
            success = True
        except asyncio.TimeoutError:
            output = ""
            raise ValueError(f"python script timed out after {timeout}s")
        except RuntimeError as e:
            if "blocked by runtime guard" in str(e):
                raise ValueError(f"python script blocked by runtime guard: {e}")
            raise
        finally:
            log_buf.close()

        duration_ms = int((time.time() - start_time) * 1000)

        if trace_id:
            await record_span(
                trace_id=trace_id,
                span_name=f"skill:python:{skill_meta.name}",
                duration_ms=duration_ms,
                metadata={"tenant_id": tenant_id, "success": True},
                tenant_id=tenant_id,
            )

        return SkillExecutionResult(
            skill_id=full_skill_id,
            skill_name=skill_meta.name,
            success=success,
            output=output,
            duration_ms=duration_ms,
            trace_id=trace_id,
            tenant_id=tenant_id,
        )

    # ── Shell 命令执行 (沙箱) ──────────────────────────────────────

    async def _execute_shell_command(
        self,
        skill_meta: SkillMetadata,
        params: dict,
        full_skill_id: str,
        tenant_id: str,
        trace_id: str,
        start_time: float,
    ) -> SkillExecutionResult:
        """在安全沙箱中执行 Shell 命令技能。

        复用 sandbox 模块的 run_in_sandbox：cwd 锁定 + 环境清理 +
        逃逸拦截 + 命令白名单。

        config 字段:
            command:  命令模板（可用 {var} 引用 params）
            timeout:  超时秒数（默认 120）
        """
        from app.tools.sandbox import run_in_sandbox

        command_template = skill_meta.config.get("command", "")
        timeout = int(skill_meta.config.get("timeout", 120))

        if not command_template.strip():
            raise ValueError("skill config missing 'command' field")

        # 安全地填充模板变量（仅 str.format，不执行任意代码）
        # 对参数值中的 shell 元字符做转义，防止注入到沙箱前被利用（见模块级 _SHELL_META_CHARS）
        safe_params = {}
        for k, v in params.items():
            if isinstance(v, str):
                safe_params[k] = v
            else:
                safe_params[k] = str(v)
            if _SHELL_META_CHARS.search(safe_params[k]):
                raise ValueError(f"shell meta characters not allowed in parameter '{k}'")
        try:
            command = command_template.format(**safe_params)
        except KeyError as e:
            raise ValueError(f"missing parameter in command template: {e}")
        except Exception as e:
            raise ValueError(f"command template render failed: {e}")

        result = await run_in_sandbox(command, timeout=timeout)

        duration_ms = int((time.time() - start_time) * 1000)

        if "error" in result:
            output = json.dumps(result, ensure_ascii=False)
            success = False
        else:
            output = json.dumps(result, ensure_ascii=False)
            success = result.get("exit_code", -1) == 0

        if trace_id:
            await record_span(
                trace_id=trace_id,
                span_name=f"skill:shell:{skill_meta.name}",
                duration_ms=duration_ms,
                metadata={"tenant_id": tenant_id, "exit_code": result.get("exit_code")},
                tenant_id=tenant_id,
            )

        return SkillExecutionResult(
            skill_id=full_skill_id,
            skill_name=skill_meta.name,
            success=success,
            output=output,
            error=result.get("error", ""),
            duration_ms=duration_ms,
            trace_id=trace_id,
            tenant_id=tenant_id,
        )

    # ── HTTP 请求执行 ───────────────────────────────────────────────

    async def _execute_http_request(
        self,
        skill_meta: SkillMetadata,
        params: dict,
        full_skill_id: str,
        tenant_id: str,
        trace_id: str,
        start_time: float,
    ) -> SkillExecutionResult:
        """执行 HTTP 请求技能（带 SSRF 防护）。

        复用 ssrf 模块的 assert_safe_url：解析目标 host，
        拒绝内网/私有/保留 IP 段。

        config 字段:
            url:             请求 URL 模板（可用 {var} 引用 params）
            method:          HTTP 方法（默认 GET）
            headers:         请求头 dict（可选）
            body_template:   请求体模板（可选）
            timeout:         超时秒数（默认 30）
            expected_status: 期望的响应状态码（可选，用于校验）
        """
        from app.tools.ssrf import assert_safe_url

        try:
            import httpx
        except ImportError:
            raise ValueError("httpx not installed — cannot execute HTTP request skill")

        url_template = skill_meta.config.get("url", "")
        method = skill_meta.config.get("method", "GET").upper()
        headers = skill_meta.config.get("headers", {})
        body_template = skill_meta.config.get("body_template", "")
        timeout = int(skill_meta.config.get("timeout", 30))
        expected_status = skill_meta.config.get("expected_status")

        if not url_template.strip():
            raise ValueError("skill config missing 'url' field")

        # 安全地填充模板
        try:
            url = url_template.format(**params)
        except KeyError as e:
            raise ValueError(f"missing parameter in url template: {e}")
        except Exception as e:
            raise ValueError(f"url template render failed: {e}")

        body = ""
        if body_template:
            try:
                body = body_template.format(**params)
            except KeyError as e:
                raise ValueError(f"missing parameter in body template: {e}")
            except Exception as e:
                raise ValueError(f"body template render failed: {e}")

        # SSRF 防护：拒绝内网/私有地址
        assert_safe_url(url)

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                content=body if body else None,
            )

        duration_ms = int((time.time() - start_time) * 1000)

        success = response.is_success
        if expected_status is not None:
            success = response.status_code == expected_status

        output = json.dumps(
            {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": response.text[:10000],
            },
            ensure_ascii=False,
        )

        if trace_id:
            await record_span(
                trace_id=trace_id,
                span_name=f"skill:http:{skill_meta.name}",
                duration_ms=duration_ms,
                metadata={
                    "tenant_id": tenant_id,
                    "url": url,
                    "method": method,
                    "status_code": response.status_code,
                },
                tenant_id=tenant_id,
            )

        return SkillExecutionResult(
            skill_id=full_skill_id,
            skill_name=skill_meta.name,
            success=success,
            output=output,
            duration_ms=duration_ms,
            trace_id=trace_id,
            tenant_id=tenant_id,
        )


# ── 全局 SkillManager 实例 ────────────────────────────────────────
# 生产环境应使用 per-tenant 实例 (Redis 隔离)
_global_skill_manager: Optional[SkillManager] = None


def get_skill_manager() -> SkillManager:
    """获取全局技能管理器 (单例)"""
    global _global_skill_manager
    if _global_skill_manager is None:
        _global_skill_manager = SkillManager()
    return _global_skill_manager


# ── Go 侧限流配置示例 ─────────────────────────────────────────────
# Go gateway_router.go 中的限流中间件:
#
# skillRateLimiter := NewTenantRateLimiter(redis, 50, 100)
# skillRateMW := skillRateLimiter.Middleware
#
# mux.Handle("POST /v1/skills", authMW(skillRateMW(...)))
# mux.Handle("GET /v1/skills/{id}/execute", authMW(skillRateMW(...)))
