"""核心本地工具集（Python 端）。

首波迁移：filesystem / shell / search(grep) / web。
后续可继续扩展 memory / pm / skill 等工具。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from app.tools.registry import registry

logger = logging.getLogger(__name__)


def _safe_path(path: str, root: str) -> Path:
    """防止路径穿越，返回绝对路径。"""
    base = Path(root).resolve()
    target = (base / path).resolve()
    # 必须加 trailing separator，否则 /app 会误匹配 /appdata
    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError("path escapes root")
    return target


#: 一次返回的最大行数（未显式传 `limit` 时）。与 Reasonix 的 `readFileDefaultLimit` 同值：
#: 分页的意义是"给一个能继续的窗口"，不是"一次读完"。
READ_FILE_DEFAULT_LIMIT = 2000

#: `read_file` **单次返回的字节上限**。
#:
#: 行数上限管不住"超长单行"：压缩产物、生成代码、minified JS 常有一行几十万字符，
#: `limit=2000` 行一次就可能吐出好几 MB —— 那是把整个文件灌进上下文，比"逐页翻"
#: 更糟，因为连翻页的机会都没有。字节是唯一能兜住这种形态的闸门。
#: 超限时按**行边界**收缩，并给出真实的续读 `offset`（不能用 `offset + limit`，
#: 那会跳过被截掉的部分）。
READ_FILE_MAX_BYTES = 200 * 1024


def _trim_to_bytes(text: str, budget: int) -> str:
    """按**行边界**把文本裁进字节预算（宁可少一行，也不留半行）。

    单独一行就超预算时（minified 文件）只能硬切，但截在**字符**边界上 ——
    按字节切会把一个多字节字符劈成两半，回灌给模型就是乱码。
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= budget:
        return text
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        cost = len(line.encode("utf-8", errors="replace")) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    if not kept:
        return encoded[:budget].decode("utf-8", errors="ignore")
    return "\n".join(kept)

#: `grep_files` 的**单文件**命中上限。没有它时，一个高频词（如 `user`）会把
#: 单文件的几百条命中全部倒进上下文 —— 总条数上限 200 拦不住这种"一个文件吃满额度"。
GREP_MAX_PER_FILE = 20
#: `grep_files` 单行文本的最大字符数。压缩过的产物、生成代码常有几万字符的单行，
#: 原样回灌等于把整个文件塞进上下文。截断后仍保留可定位的前缀。
GREP_LINE_MAX_CHARS = 240
#: `grep_files` 单文件**扫描字节预算**。超出部分不扫，但**会在结果里如实报出**
#: （`partial_scan`）。
#:
#: 此前每个候选文件都走 `read(limit=0)`（读全文）再逐行匹配 —— 在一个含
#: `node_modules`/构建产物/日志的仓库里，这会把几百 MB 读进内存，而命中的
#: 往往只是其中几行。所以预算必须有；但"整份跳过"是错的处理方式：关键词
#: 经常就在文件前几 KB（配置项、日志头部、生成文件的开头），跳过等于让模型
#: 误判"确实没有匹配"。现在改为扫前 N 字节 + 明确标注只扫了一部分。
GREP_MAX_FILE_BYTES = 2 * 1024 * 1024
#: `grep_files` 单页扫描的行数。**分页扫描**（而非全文读入）让内存占用与文件大小
#: 脱钩：一次只在内存里驻留一页，逐页匹配、命中即停。
GREP_SCAN_PAGE_LINES = 2000


async def read_file(path: str, offset: int = 0, limit: int = READ_FILE_DEFAULT_LIMIT) -> dict[str, Any]:
    """读文件（分页）。

    路径一律 clamp 到当前工作区 —— 此前签名里有个 `root` 参数，模型以为能改根目录，
    实现根本不看它（已移除）。

    **续读**：`offset` 是 0-based 行偏移，`limit` 是本次最多返回的行数。被截断时返回值
    带 `truncated`，且正文末尾附一行 `[more lines below; pass offset=N to continue]` ——
    模型据此继续读，不必靠比较 `offset + limit` 与 `total_lines` 去猜还有没有下文。

    **"非必要不全读"的引导放在返回值里**，而不是只写在工具描述里：模型真正会读的是
    工具结果，描述在长上下文里早就被压掉了。所以：

    * 整篇读完（`truncated=False`）且是小文件时，附一句"已读完"，避免它再从头读一遍；
    * 被截断时，除了给出续读 `offset`，还提示**如果只是在找某段内容，先用 `grep_files`
      定位行号再按区间读** —— 逐页翻完一个几千行的文件，代价是把整个文件灌进上下文；
    * 文件大到不统计总行（后端返回 `total_lines=-1`）时，明确说明"总行数未知"并直接
      建议改用 grep 定位，而不是让模型以为 `-1` 是某种行数。
    """
    from app.backends.context import get_backend
    from app.tools.fs_guard import observe
    from app.tools.sandbox import safe_join

    # 文件读经**后端**（默认 = 本地工作区，行为与迁移前一致）
    result = await get_backend().read(path, offset=offset, limit=limit)
    if result.error:
        return {"error": result.error}
    # 横切逻辑留在工具层：read-before-write 观测（写入校验依赖它）
    observe(safe_join(path))  # 记录版本，供 write/edit 的 read-before-write 校验
    content = result.content
    total_unknown = result.total_lines < 0

    # ── 字节闸门 ──
    # 行数上限管不住"超长单行"：`limit=2000` 行在 minified 文件里可能是几 MB。
    # 裁剪后必须用**实际保留的行数**算续读起点 —— 沿用 `offset + limit` 会跳过
    # 被裁掉的那部分，模型于是永远读不到它们（静默丢内容，比报错更糟）。
    byte_capped = False
    if len(content.encode("utf-8", errors="replace")) > READ_FILE_MAX_BYTES:
        content = _trim_to_bytes(content, READ_FILE_MAX_BYTES)
        byte_capped = True
    kept_lines = len(content.splitlines())
    truncated = result.truncated or byte_capped

    if truncated:
        # 截断提示由**产生截断的这一层**给出，不在这里重算 `truncated`。
        next_offset = result.offset + kept_lines
        reason = "此行窗口单次返回已达字节上限" if byte_capped else "下面还有内容"
        content = (
            f"{content}\n[{reason}；pass offset={next_offset} to continue. "
            f"如果要找的是某段内容，先用 grep_files 定位行号再读区间，"
            f"不要逐页翻完整个文件]"
        )
    payload: dict[str, Any] = {
        "path": result.path,
        "total_lines": result.total_lines,
        "offset": result.offset,
        "limit": result.limit,
        "truncated": truncated,
        "content": content,
    }
    if byte_capped:
        # 与"行截断"分开报：行截断靠 `offset + limit` 就能续读，字节截断必须
        # 认 `next_offset` —— 两者混为一谈会让模型跳过被裁掉的中间段。
        payload["returned_lines"] = kept_lines
        payload["byte_limit"] = READ_FILE_MAX_BYTES
        payload["next_offset"] = result.offset + kept_lines
    if total_unknown:
        # `-1` 是"未统计"的哨兵（后端对大文件跳过全文扫描，见 `_LINE_COUNT_MAX_BYTES`）。
        # 不解释的话，模型会把它当成真行数去做 `offset + limit` 的比较。
        payload["total_lines_known"] = False
        payload["hint"] = (
            "文件较大，后端未统计总行数（total_lines=-1）。"
            "建议先用 grep_files 定位关键行，再用 offset/limit 只读需要的区间。"
        )
    elif offset == 0 and not truncated:
        # 一次就读完了：明说，省掉模型"是不是还有下文"的再读一次。
        # **放在 `hint` 而不是拼进 `content`**：`content` 的契约是"文件原文"，
        # 往里加说明会污染它 —— 例如把内容再写回去就会带上这句话。
        payload["hint"] = f"这是完整文件，共 {result.total_lines} 行。"
    return payload


async def read_image(path: str, max_bytes: int = 0) -> dict[str, Any]:
    """把工作区里的图片读成 data-URL（供 vision 模型）。

    类型**按字节嗅探**（不看扩展名）；超过上限或装不进 `MAX_DIM` 的一律**明确拒绝**，
    绝不把超限图片原样发给 provider —— 那会被当成 unsupported format，并连带毁掉
    这个会话之后的每一轮。降采样/重编码的裁决统一在 `app.media.image_fit`。

    路径同样 clamp 到工作区（`root` 参数已移除，理由与 `read_file` 相同）。
    """
    import base64

    from app.backends.context import get_backend
    from app.media.image_fit import MAX_IMAGE_BYTES, ImageUnfit, fit, sniff_mime
    from app.tools.fs_guard import observe
    from app.tools.sandbox import safe_join

    limit = int(max_bytes) if max_bytes and max_bytes > 0 else MAX_IMAGE_BYTES
    backend = get_backend()
    target = safe_join(path)  # 沙箱隔离（S 安全修复）

    # 大小与字节都经后端（默认 = 本地工作区）
    info = await backend.stat(path)
    if info is None:
        return {"error": f"file not found: {path}"}
    if info.size <= 0:
        return {"error": f"image is empty: {path}"}
    if info.size > limit:
        return {"error": f"image too large ({info.size} bytes, max {limit})"}
    blob = await backend.read_bytes(path)
    if blob.error:
        return {"error": blob.error}

    # 类型由**字节**说了算：扩展名是声明，声明错了到 provider 那里就是不符的 media_type。
    mime = sniff_mime(blob.data)
    if not mime:
        return {"error": f"unsupported image data: {path} is not PNG/JPEG/GIF/WebP"}
    try:
        data, mime = fit(blob.data, mime)
    except ImageUnfit as exc:
        return {"error": f"{path}: {exc}"}
    observe(target)
    return {
        "path": str(target),
        "media_type": mime,
        "bytes": len(data),
        "data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
    }


async def write_file(
    path: str, content: str, mode: str = "overwrite"
) -> dict[str, Any]:
    """写文件（会建父目录；写入前自动快照供 /undo 回滚）。

    路径 clamp 到工作区（`root` 参数已移除，理由与 `read_file` 相同）。

    `mode`：

    - `overwrite`（默认）：整篇替换，**原子**落盘；
    - `append`：追加到末尾，不存在则创建。

    为什么要 `append`：模型生成一个长文件时，工具参数本身就是一段 JSON，输出会在
    `max_tokens` 处**从中间截断** —— JSON 不合法 ⇒ 工具被拒 ⇒ 重试又撞同一个上限，
    形成死循环。分块写把"写一个长文件"拆成"写若干块"，每块的输出量都在预算内，
    于是长文件不再依赖单次输出撑满上下文。
    """
    from app.agent import undo_stack
    from app.backends.context import get_backend
    from app.tools.context import get_session_id
    from app.tools.fs_guard import check_before_write, observe
    from app.tools.sandbox import safe_join

    if mode not in ("overwrite", "append"):
        # 明确的取值校验：静默把拼错的值当 overwrite 会**清掉已有文件**，
        # 这是最不该"容错"的一处。
        return {"error": f"invalid mode: {mode!r} (expected 'overwrite' or 'append')"}

    target = safe_join(path)  # 沙箱隔离
    # 横切逻辑留在工具层，且**顺序不变**：冲突检查 → undo 快照 → 写入
    conflict = check_before_write(target)
    if conflict:
        return {"error": conflict}
    # 写入前快照：`/undo` 据此**真正恢复**文件（此前 /undo 只是回显一个字符串）。
    # 快照存不下（文件过大 / 存储不可用）时**当场告知"本次不可撤销"**，而不是事后才发现。
    undo_note = await undo_stack.snapshot_before_write(get_session_id(), target, "write_file")
    # 写入经**后端**（默认 = 本地工作区；建父目录也由后端负责）
    backend = get_backend()
    written = await (backend.append(path, content) if mode == "append" else backend.write(path, content))
    if written.error:
        return {"error": written.error}
    # 写成功后**刷新读取签名**：否则同一会话里"写一个文件 → 再写/再编辑它"必然撞上
    # `file changed since it was last read` —— 因为签名记录的是写入**前**的版本，
    # 而写入本身改了 mtime/size。模型看到的表现为"我刚写完就不让我改了"，
    # 于是退回整篇 write_file 重试（又撞 max_tokens 截断）。这是长任务里
    # 最伤连续性的一个坑：每写一次就锁死该文件。
    observe(target)
    result: dict[str, Any] = {
        "path": written.path,
        "bytes": written.bytes_written,
        "mode": mode,
    }
    if undo_note:
        result["undo_warning"] = undo_note
    return result


async def execute_command(command: str, timeout: int = 30) -> dict[str, Any]:
    """在沙箱内执行一条 shell 命令。

    工作目录固定为当前用户的工作区：此前签名里有 `cwd`，schema 说它能改目录，
    实现却忽略它 —— 模型据此重试过多次（已移除）。
    """
    from app.tools.sandbox import run_in_sandbox

    return await run_in_sandbox(command, timeout=timeout)


async def grep_files(
    query: str,
    root: str = ".",
    glob: str = "",
    max_results: int = 200,
    max_per_file: int = GREP_MAX_PER_FILE,
) -> dict[str, Any]:
    """按正则搜索文件内容（匹配与读取都经后端）。

    候选文件由 `**/{glob}` 限定 → 逐个**分页**扫描 → 逐行匹配 → 命中达上限即返回。

    **为什么分页而不是读全文**：旧实现对每个候选文件调 `read(limit=0)` —— 在含
    `node_modules`、构建产物、日志目录的仓库里，这等于把几百 MB 一次性读进内存，
    只为找出其中几行。现在按 `GREP_SCAN_PAGE_LINES` 逐页取，内存占用与文件大小
    脱钩，命中够了就停，后面几页根本不用读。

    **三道上限**：`max_results` 管总量，`max_per_file` 防"单个高频文件吃满额度"，
    `GREP_MAX_FILE_BYTES` 限制单个文件只扫前 N 字节；`GREP_LINE_MAX_CHARS` 防"一行
    几万字符"（压缩产物、生成代码）原样进上下文。只扫了一部分的文件**如实报进
    `partial_scan`**：静默报"没命中"会让模型以为"确实没有匹配"，而真相是"这个文件
    没扫完" —— 两者后续该做的事完全不同（缩小范围续扫 vs 换关键词）。
    """
    from app.backends.context import get_backend

    try:
        pattern = re.compile(query)
    except re.error as exc:
        return {"error": f"invalid pattern: {exc}", "count": 0, "matches": []}

    backend = get_backend()
    globbed = await backend.glob(f"**/{glob or '*'}", path=root or ".")
    if globbed.error:
        return {"error": globbed.error, "count": 0, "matches": []}

    matches: list[dict[str, Any]] = []
    partial_files: list[dict[str, Any]] = []
    for candidate in globbed.paths:
        info = await backend.stat(candidate)
        if info is None or not info.is_file:
            continue
        # 大文件**不再跳过**，改为只扫前 `GREP_MAX_FILE_BYTES` 字节。
        #
        # 旧行为是整份 skip：模型拿到 `skipped_large` 只知道"这个文件没搜"，
        # 却常常据此断定"确实没有匹配" —— 而真相是关键词可能就在前几 KB 里。
        # 扫前 N MB 覆盖了绝大多数真实场景（配置、日志头部、生成文件的开头），
        # 代价有界，且**截断如实报出**，模型可以据此决定是否改用更精确的路径。
        budget = GREP_MAX_FILE_BYTES
        scanned = 0
        partial = False
        # 本文件的命中计数：达到 `max_per_file` 就整份文件收手，避免单个高频文件
        # 把总额度吃满（总上限 `max_results` 只能保证"总量"，管不住"分布"）。
        file_hits = 0
        offset = 0
        done = False
        while not done:
            read = await backend.read(candidate, offset=offset, limit=GREP_SCAN_PAGE_LINES)
            if read.error:
                logger.debug("Cannot read file %s, skipping", candidate)
                break
            for idx, line in enumerate(read.content.splitlines(), start=offset + 1):
                if not pattern.search(line):
                    continue
                # 单行截断：压缩产物/生成代码常有几万字符的单行，原样回灌等于塞进整个文件。
                if len(line) > GREP_LINE_MAX_CHARS:
                    line = f"{line[:GREP_LINE_MAX_CHARS]}…[+{len(line) - GREP_LINE_MAX_CHARS} chars]"
                matches.append({"path": candidate, "line": idx, "text": line})
                file_hits += 1
                if len(matches) >= max_results:
                    return _grep_result(matches, partial_files)
                if file_hits >= max_per_file:
                    done = True
                    break
            scanned += len(read.content.encode("utf-8", errors="replace"))
            if scanned >= budget:
                # 字节预算用完：文件还有后续内容就不再读，并记下"只扫了一部分"。
                partial = info.size > scanned
                break
            if done or not read.truncated:
                break
            offset += len(read.content.splitlines()) or GREP_SCAN_PAGE_LINES
        if partial:
            partial_files.append(
                {"path": candidate, "scanned_bytes": scanned, "total_bytes": info.size}
            )
    return _grep_result(matches, partial_files)


def _grep_result(
    matches: list[dict[str, Any]], partial_files: list[dict[str, Any]]
) -> dict[str, Any]:
    """组装 `grep_files` 的返回体。

    把"只扫了一部分"与"完全没有命中"分开表达：两者在模型眼里都是"没找到"，
    但后续动作截然不同 —— 前者应该缩小路径或按 offset 续扫，后者该换关键词。
    """
    result: dict[str, Any] = {"count": len(matches), "matches": matches}
    if partial_files:
        result["partial_scan"] = partial_files[:5]
        result["partial_scan_count"] = len(partial_files)
        result["note"] = (
            f"有 {len(partial_files)} 个文件只扫描了前 {GREP_MAX_FILE_BYTES // 1024} KB"
            "（未扫完）。这些文件里的命中可能不完整 —— 目标若在里面，"
            "请缩小 root/glob 让它落到预算内，或直接用 read_file 分段查看。"
        )
    return result


async def search_files(
    pattern: str, root: str = ".", max_results: int = 200
) -> dict[str, Any]:
    """按文件名模式搜索（匹配经后端）。

    与 `glob_files` 的区别是**自动递归**（`**/` 前缀）—— 这是既有行为差异，保留。
    """
    from app.backends.context import get_backend

    backend = get_backend()
    globbed = await backend.glob(f"**/{pattern or '*'}", path=root or ".")
    if globbed.error:
        return {"error": globbed.error, "count": 0, "files": []}

    files: list[str] = []
    for candidate in globbed.paths:
        info = await backend.stat(candidate)
        if info is not None and info.is_file:
            files.append(candidate)
        if len(files) >= max_results:
            break
    return {"count": len(files), "files": files}


async def execute_python(code: str, timeout: int = 30) -> dict[str, Any]:
    """执行一段 Python 代码（沙箱内）。

    为什么不是 `python -c <代码>`：那要把源码**拼进 shell 命令行**，于是必须自己处理引号、
    换行、反斜杠的转义 —— `json.dumps` 只解决了一部分（POSIX 下可行，Windows 的 cmd.exe
    规则不同，含双引号/换行的代码会直接执行失败或截断）。

    这里改成 **base64 传递**：`-c` 的参数整体用双引号包住，内部只有 base64 字母表
    （`A-Za-z0-9+/=`，不含引号与空白），因此两种 shell 下的引号解析都无歧义。
    代价是命令行长一点（约 4/3 倍），换来的是**跨平台确定性**。
    """
    if not code:
        return {"error": "code is required"}
    import base64

    payload = base64.b64encode(code.encode("utf-8")).decode("ascii")
    command = (
        'python -c "import base64;'
        f"exec(base64.b64decode('{payload}').decode('utf-8'))\""
    )
    return await execute_command(command=command, timeout=timeout)


# ── 已删除：core 里的 web_fetch ──
#
# 这里曾有一份 `web_fetch` 实现（httpx + assert_safe_url），但它**从未注册**，
# 而 `app/tools/web.py` 里有一份更完整的同名实现（HTML→Markdown、字符上限、错误语义）。
# 两份实现并存 = 必然漂移（规划 §5.3 的"两份白名单必然漂移"是同一类错）。
# 只保留 web.py 的那一份；本模块不再有出网能力。


# 注册工具（schema 与 Go 侧保持兼容）
registry.register(
    name="read_file",
    description=(
        "Read a text file from the workspace with pagination. Use `offset`/`limit` to page "
        "through large files; a truncated result says so in a trailer and names the offset to "
        "continue from."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"},
            "offset": {"type": "integer", "default": 0, "description": "Line to start from (0-based)"},
            "limit": {
                "type": "integer",
                "default": 2000,
                "description": "Max lines to return (default 2000)",
            },
        },
        "required": ["path"],
    },
    handler=read_file,
)

registry.register(
    name="write_file",
    description=(
        "Write text to a file in the workspace (parent directories are created). "
        "This is for work-in-progress files; to hand the user a file they can open or "
        "download, use media_create instead. "
        "For a long file, do NOT try to emit it in one call: passing mode='append' lets you "
        "write it in chunks, so the tool arguments never hit the max_tokens truncation that "
        "leaves invalid JSON and rejects the whole write."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"},
            "content": {"type": "string", "description": "Content to write (or to append)"},
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append"],
                "default": "overwrite",
                "description": (
                    "overwrite = replace the whole file atomically (default); "
                    "append = add to the end, creating the file if absent. "
                    "Use append for long files written in several chunks."
                ),
            },
        },
        "required": ["path", "content"],
    },
    handler=write_file,
)

registry.register(
    name="shell_exec",
    description=(
        "Run a shell command in the sandbox. The working directory is always the workspace "
        "(it cannot be changed). Returns stdout/stderr/exit_code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {"type": "integer", "default": 30, "description": "Timeout in seconds"},
        },
        "required": ["command"],
    },
    handler=execute_command,
)

registry.register(
    name="grep_files",
    description="Regex-search the contents of files under a workspace directory.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Regular expression to match against each line"},
            "root": {"type": "string", "default": ".", "description": "Directory to search under (relative to the workspace)"},
            "glob": {"type": "string", "default": "", "description": "Only scan files matching this glob (e.g. '*.py')"},
            "max_results": {"type": "integer", "default": 200, "description": "Stop after this many matches"},
        },
        "required": ["query"],
    },
    handler=grep_files,
)

registry.register(
    name="search_files",
    description="Search files by glob pattern",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "root": {"type": "string", "default": "."},
            "max_results": {"type": "integer", "default": 200},
        },
        "required": ["pattern"],
    },
    handler=search_files,
)

registry.register(
    name="execute_python",
    description=(
        "Run a short Python snippet in the sandbox. The code is passed as base64 so quoting "
        "works identically on every platform. For multi-step work that should call several "
        "tools in one shot, prefer run_code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute"},
            "timeout": {"type": "integer", "default": 30, "description": "Timeout in seconds"},
        },
        "required": ["code"],
    },
    handler=execute_python,
)

registry.register(
    name="read_image",
    description=(
        "Read an image file (PNG/JPEG/GIF/WebP, sniffed from the bytes rather than the "
        "extension) as a base64 data-URL for vision-capable models. Images larger than "
        "MAX_DIM (1568px) are downscaled to fit before encoding."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Image path relative to the workspace root"},
            "max_bytes": {
                "type": "integer",
                "default": 10485760,
                "description": "Max image bytes (default 10 MB)",
            },
        },
        "required": ["path"],
    },
    handler=read_image,
)
