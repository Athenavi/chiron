"""web_search / web_fetch — 网页发现与抓取（对应 deepseek-harness dsh-tool-web）

- web_search：查询返回 answer/来源列表（默认 DuckDuckGo HTML 接口，无 key）
- web_fetch：抓取 URL，HTML → markdown 渲染（bs4 递归转换）

WebProvider seam：默认 DuckDuckGoProvider，可通过 set_web_provider 替换
（对应 deepseek 的多 provider 路由，如 Exa/Perplexity）。
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from app.tools.registry import registry
from app.tools.ssrf import assert_safe_url, fetch_url_safe

logger = logging.getLogger(__name__)

SEARCH_MAX_RESULTS = 8
FETCH_MAX_CHARS = 200_000


class WebProvider:
    """网页能力 seam：search + fetch。"""

    async def search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def fetch(self, url: str, max_chars: int) -> dict[str, Any]:
        raise NotImplementedError


class DuckDuckGoProvider(WebProvider):
    """无 key 的 DuckDuckGo HTML 搜索 + 通用抓取。"""

    async def search(
        self, query: str, max_results: int = SEARCH_MAX_RESULTS
    ) -> list[dict[str, Any]]:
        from app.config import settings
        assert_safe_url("https://html.duckduckgo.com/html/")  # SSRF 防护（S4）
        async with httpx.AsyncClient(timeout=settings.http_timeout_web, follow_redirects=True) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/", data={"q": query}
            )
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[dict[str, Any]] = []
        for a in soup.select("a.result__a")[:max_results]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            url = self._decode_redirect(href)
            snippet = ""
            parent = a.find_parent("div", class_="result")
            if parent:
                sn = parent.select_one("a.result__snippet")
                snippet = sn.get_text(strip=True) if sn else ""
            results.append({"title": title, "url": url, "snippet": snippet})
        return results

    @staticmethod
    def _decode_redirect(href: str) -> str:
        """DuckDuckGo 的 /l/?uddg= 重定向链接解码为真实 URL。"""
        if "uddg=" in href:
            parsed = parse_qs(urlparse(href).query)
            if parsed.get("uddg"):
                return unquote(parsed["uddg"][0])
        if href.startswith("//"):
            return "https:" + href
        return href

    async def fetch(self, url: str, max_chars: int = FETCH_MAX_CHARS) -> dict[str, Any]:
        from app.config import settings
        assert_safe_url(url)  # SSRF 防护（S4）
        async with httpx.AsyncClient(timeout=settings.http_timeout_web, follow_redirects=True) as client:
            resp = await fetch_url_safe(client, url)
        content_type = resp.headers.get("content-type", "")
        body = resp.text
        if "html" in content_type.lower() or body.lstrip().startswith("<"):
            rendered = html_to_markdown(body)
        else:
            rendered = body
        return {
            "url": str(resp.url),
            "status_code": resp.status_code,
            "content_type": content_type,
            "content": rendered[:max_chars]
            + ("\n[truncated]" if len(rendered) > max_chars else ""),
        }


# 默认 provider（可通过 set_web_provider 替换）
_provider: WebProvider = DuckDuckGoProvider()


def set_web_provider(provider: WebProvider | None) -> None:
    """替换 WebProvider（测试/多 provider 配置用）。"""
    global _provider
    _provider = provider or DuckDuckGoProvider()


def html_to_markdown(html: str) -> str:
    """HTML → markdown 简化渲染（bs4 递归转换）。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    root = soup.body or soup

    out: list[str] = []

    def walk(node: Any, list_stack: list[str] | None = None) -> None:
        if isinstance(node, str):
            text = node.strip()
            if text:
                out.append(_inline(text))
            return
        name = node.name
        if name in ("script", "style"):
            return
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(name[1])
            out.append(
                "\n"
                + "#" * level
                + " "
                + _inline(node.get_text(" ", strip=True))
                + "\n"
            )
            return
        if name == "p":
            for child in node.children:
                walk(child)
            out.append("\n")
            return
        if name == "br":
            out.append("\n")
            return
        if name in ("ul", "ol"):
            out.append("\n")
            for li in node.find_all("li", recursive=False):
                marker = "- " if name == "ul" else "1. "
                text = _inline(li.get_text(" ", strip=True))
                out.append(f"  {marker}{text}\n")
            out.append("\n")
            return
        if name == "pre":
            code = node.get_text()
            out.append("\n```\n" + code.strip("\n") + "\n```\n")
            return
        if name == "blockquote":
            text = node.get_text(" ", strip=True)
            out.append("\n> " + text.replace("\n", "\n> ") + "\n")
            return
        if name == "table":
            out.append("\n")
            for tr in node.find_all("tr"):
                cells = [
                    td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])
                ]
                out.append("| " + " | ".join(cells) + " |\n")
            out.append("\n")
            return
        if name == "a":
            href = node.get("href", "")
            text = node.get_text(" ", strip=True)
            if text:
                out.append(f"[{text}]({href})" if href else text)
            return
        # 其它容器：递归子节点
        for child in node.children:
            walk(child)

    for child in root.children:
        walk(child)

    text = "\n".join(line for line in out if line)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _inline(text: str) -> str:
    """保留链接/强调的轻量行内清理。"""
    return re.sub(r"[ \t]+", " ", text).strip()


async def web_search(
    query: str, max_results: int = SEARCH_MAX_RESULTS
) -> dict[str, Any]:
    """Search the web and return source titles/URLs/snippets."""
    if not query.strip():
        return {"error": "query is required"}
    try:
        results = await _provider.search(query, max_results)
    except Exception as e:  # noqa: BLE001
        logger.warning("web_search failed: %s", e)
        return {"error": f"web search unavailable: {e}"}
    return {"query": query, "count": len(results), "results": results}


async def web_fetch(url: str, max_chars: int = FETCH_MAX_CHARS) -> dict[str, Any]:
    """Fetch a URL; HTML bodies are rendered to markdown."""
    if not url:
        return {"error": "url is required"}
    try:
        result = await _provider.fetch(url, max_chars)
    except httpx.HTTPStatusError as e:
        return {"url": url, "status_code": e.response.status_code, "content": ""}
    except Exception as e:  # noqa: BLE001
        logger.warning("web_fetch failed: %s", e)
        return {"error": "web fetch unavailable"}
    return result


registry.register(
    name="web_search",
    description="Search the web for current information. Returns a list of source titles, URLs and snippets. Follow up with web_fetch for full content.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {
                "type": "integer",
                "default": SEARCH_MAX_RESULTS,
                "description": "Max sources to return",
            },
        },
        "required": ["query"],
    },
    handler=web_search,
)

registry.register(
    name="web_fetch",
    description="Fetch the content of a specific HTTP(S) URL (e.g. a result from web_search). HTML is rendered to markdown; cite the URL when using its content.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "default": FETCH_MAX_CHARS},
        },
        "required": ["url"],
    },
    handler=web_fetch,
)
