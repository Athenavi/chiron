# 文档解析器 - 支持 PDF/MD/TXT/CSV/DOCX
import io
import logging

logger = logging.getLogger(__name__)


class DocumentParser:
    """文档解析器，将各种格式转换为纯文本"""

    def parse(self, content: bytes, file_type: str, filename: str = "") -> dict:
        """
        解析文档内容

        Args:
            content: 文件内容（bytes）
            file_type: 文件类型 (pdf/md/txt/csv/docx)
            filename: 文件名

        Returns:
            dict: {
                "text": str,           # 提取的文本内容
                "char_count": int,     # 字符数（用于计费）
                "page_count": int,     # 页数（PDF）
                "metadata": dict,      # 元数据
                "error": str|None      # 错误信息
            }
        """
        try:
            if file_type == "txt":
                return self._parse_txt(content)
            elif file_type == "md":
                return self._parse_markdown(content)
            elif file_type == "csv":
                return self._parse_csv(content)
            elif file_type == "pdf":
                return self._parse_pdf(content)
            elif file_type == "docx":
                return self._parse_docx(content)
            else:
                return self._parse_txt(content)  # fallback
        except Exception as e:
            logger.error(f"文档解析失败: {file_type}, {filename}, error={e}")
            return {
                "text": "",
                "char_count": 0,
                "page_count": 0,
                "metadata": {},
                "error": str(e),
            }

    def _parse_txt(self, content: bytes) -> dict:
        """解析纯文本"""
        text = content.decode("utf-8", errors="ignore")
        return {
            "text": text,
            "char_count": len(text),
            "page_count": 1,
            "metadata": {"format": "txt"},
            "error": None,
        }

    def _parse_markdown(self, content: bytes) -> dict:
        """解析 Markdown（保留结构）"""
        text = content.decode("utf-8", errors="ignore")
        # 移除 Markdown 语法标记，保留文本
        # 简单处理：保留原文，让分块器处理
        return {
            "text": text,
            "char_count": len(text),
            "page_count": 1,
            "metadata": {"format": "markdown"},
            "error": None,
        }

    def _parse_csv(self, content: bytes) -> dict:
        """解析 CSV"""
        import csv
        import io

        text = content.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))

        rows = []
        headers = []
        for i, row in enumerate(reader):
            if i == 0:
                headers = row
            else:
                # 将每行转换为可读文本
                if headers:
                    pairs = [f"{h}: {v}" for h, v in zip(headers, row, strict=False) if v]
                    rows.append("; ".join(pairs))
                else:
                    rows.append(", ".join(row))

        formatted_text = "\n".join(rows)
        return {
            "text": formatted_text,
            "char_count": len(formatted_text),
            "page_count": 1,
            "metadata": {"format": "csv", "row_count": len(rows)},
            "error": None,
        }

    def _parse_pdf(self, content: bytes) -> dict:
        """解析 PDF"""
        try:
            import pymupdf  # PyMuPDF (fitz)

            doc = pymupdf.open(stream=content, filetype="pdf")
            try:
                pages = []
                for page_num in range(len(doc)):
                    page = doc[page_num]
                    text = page.get_text()
                    if text.strip():
                        pages.append(f"[Page {page_num + 1}]\n{text}")

                full_text = "\n\n".join(pages)

                return {
                    "text": full_text,
                    "char_count": len(full_text),
                    "page_count": len(pages),
                    "metadata": {"format": "pdf", "total_pages": len(doc)},
                    "error": None,
                }
            finally:
                doc.close()
        except ImportError:
            logger.warning("pymupdf 未安装，尝试使用 pdfplumber")
            return self._parse_pdf_fallback(content)

    def _parse_pdf_fallback(self, content: bytes) -> dict:
        """PDF 解析 fallback"""
        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(content)) as pdf:
                pages = []
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if text and text.strip():
                        pages.append(f"[Page {i + 1}]\n{text}")

                full_text = "\n\n".join(pages)
                return {
                    "text": full_text,
                    "char_count": len(full_text),
                    "page_count": len(pages),
                    "metadata": {"format": "pdf", "total_pages": len(pdf.pages)},
                    "error": None,
                }
        except ImportError:
            return {
                "text": "",
                "char_count": 0,
                "page_count": 0,
                "metadata": {},
                "error": "PDF 解析库未安装（需要 pymupdf 或 pdfplumber）",
            }

    def _parse_docx(self, content: bytes) -> dict:
        """解析 DOCX"""
        try:
            import docx

            doc = docx.Document(io.BytesIO(content))
            paragraphs = []
            for para in doc.paragraphs:
                text = para.text.strip()
                if text:
                    paragraphs.append(text)

            full_text = "\n\n".join(paragraphs)
            return {
                "text": full_text,
                "char_count": len(full_text),
                "page_count": 1,
                "metadata": {"format": "docx", "paragraph_count": len(paragraphs)},
                "error": None,
            }
        except ImportError:
            return {
                "text": "",
                "char_count": 0,
                "page_count": 0,
                "metadata": {},
                "error": "python-docx 未安装",
            }


class TextChunker:
    """文本分块器"""

    def chunk(
        self, text: str, chunk_size: int = 1000, chunk_overlap: int = 200
    ) -> list[dict]:
        """
        将文本分块

        Args:
            text: 文本内容
            chunk_size: 每块大小（字符数）
            chunk_overlap: 重叠大小

        Returns:
            list[dict]: [{"index": 0, "content": "...", "start": 0, "end": 1000}, ...]
        """
        if not text or not text.strip():
            return []

        chunks = []
        start = 0
        text_len = len(text)

        while start < text_len:
            end = min(start + chunk_size, text_len)

            # 尝试在句子边界分割
            if end < text_len:
                # 查找最近的句号、换行等
                for sep in ["\n\n", "\n", "。", ".", "！", "!", "？", "?"]:
                    last_sep = text.rfind(sep, start + chunk_size // 2, end)
                    if last_sep > start:
                        end = last_sep + len(sep)
                        break

            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append(
                    {
                        "index": len(chunks),
                        "content": chunk_text,
                        "start": start,
                        "end": end,
                    }
                )

            # 下一块的起始位置（考虑重叠）
            start = end - chunk_overlap if end < text_len else text_len

        return chunks


# 全局实例
document_parser = DocumentParser()
text_chunker = TextChunker()
