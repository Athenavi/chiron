"""文本分块（本地知识库最小闭环）。

口径与契约一致：**按字符**切（中文一个字算一个字符），默认 800 字一片、相邻片重叠 100 字。
重叠是为了让跨片边界的句子在两片里都能被检索到；代价是索引体积略增，这在本地小库上
不构成问题。

这里只做"机械切分"，不猜段落、不做语义分段 —— 语义分段属于 PageIndex 组件（P3），
本模块不假装有那个能力。
"""

from __future__ import annotations

DEFAULT_CHUNK_SIZE = 800
DEFAULT_OVERLAP = 100


def normalize_text(text: str) -> str:
    """统一换行、去掉 NUL（PG 的 ``text`` 不接受 ``\\x00``），头尾空白去掉。"""
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return cleaned.strip()


def chunk_text(
    text: str,
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """把 ``text`` 切成重叠的片段。

    - ``size`` 必须 > 0，``0 <= overlap < size``（否则步长 <= 0 会无限循环），违反则
      ``ValueError`` —— 宁可不干活也不死循环。
    - 空文本 / 纯空白返回 ``[]``（调用方据此判定"没有可索引内容"）。
    """
    if size <= 0:
        raise ValueError("size 必须 > 0")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap 必须满足 0 <= overlap < size")

    body = normalize_text(text)
    if not body:
        return []

    step = size - overlap
    chunks: list[str] = []
    total = len(body)
    start = 0
    while start < total:
        piece = body[start : start + size].strip()
        if piece:
            chunks.append(piece)
        if start + size >= total:
            break
        start += step
    return chunks
