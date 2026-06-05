"""
轻量级搜狗网页搜索 — 零 API 费用，中文分词优秀，反爬友好
提取自 llm_web_module/spider_and_search/crawler.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup


@dataclass
class SearchResult:
    """单条搜索结果"""
    title: str
    url: str
    snippet: str


@dataclass
class SearchResponse:
    """搜索响应"""
    query: str
    results: list[SearchResult] = field(default_factory=list)
    error: str = ""

    @property
    def context_text(self) -> str:
        """将搜索结果拼接为 LLM 可用的上下文字符串"""
        if not self.results:
            return f"(搜索\"{self.query}\"没有找到相关结果)"
        parts = [f'以下是对"{self.query}"的搜索结果:\n']
        for i, r in enumerate(self.results, 1):
            parts.append(f"[{i}] {r.title}\n    链接: {r.url}\n    摘要: {r.snippet}")
        return "\n".join(parts)


async def search_sogou(query: str, max_results: int = 5) -> SearchResponse:
    """搜狗搜索 — 直接抓取 HTML，免费，无需 API Key"""
    url = f"https://www.sogou.com/web?query={quote(query)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        return SearchResponse(query=query, error=f"搜索请求失败: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for vr in soup.select(".vrwrap"):
        h3 = vr.find("h3")
        if not h3:
            continue
        a = h3.find("a")
        if not a:
            continue

        title = a.get_text(strip=True)
        raw_url = a.get("href", "")
        if raw_url.startswith("/link?") or raw_url.startswith("/"):
            url_out = "https://www.sogou.com" + raw_url
        else:
            url_out = raw_url

        snippet = ""
        for sel in ["div.star_wrap", "div.space-txt", "div.fz-mid"]:
            elem = vr.select_one(sel)
            if elem and len(elem.get_text(strip=True)) > 5:
                snippet = elem.get_text(strip=True)
                break
        if not snippet:
            for p in vr.find_all("p"):
                text = p.get_text(strip=True)
                if len(text) > 5:
                    snippet = text
                    break

        results.append(SearchResult(
            title=title,
            url=url_out,
            snippet=snippet,
        ))

    return SearchResponse(
        query=query,
        results=results[:max_results],
    )

