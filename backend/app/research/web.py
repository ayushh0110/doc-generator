import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import trafilatura
from ddgs import DDGS
from tavily import TavilyClient

from app.config import get_settings

logger = logging.getLogger(__name__)
MAX_PAGE_BYTES = 2_000_000
MAX_PAGE_CHARS = 6000


@dataclass
class WebPage:
    title: str
    url: str
    text: str


def search_web(query: str, limit: int | None = None) -> list[WebPage]:
    settings = get_settings()
    limit = limit or settings.web_results
    if settings.tavily_api_key:
        response = TavilyClient(settings.tavily_api_key).search(
            query, max_results=limit, include_raw_content=False, search_depth="advanced"
        )
        return [
            WebPage(item.get("title") or item["url"], item["url"], item.get("content") or "")
            for item in response.get("results", [])
        ]

    # No key: DuckDuckGo for the links, and the pages fetched here.
    pages = []
    for item in DDGS().text(query, max_results=limit):
        url = item.get("href", "")
        text = fetch_text(url) or item.get("body", "")
        if text:
            pages.append(WebPage(item.get("title") or url, url, text))
    return pages


def is_public_url(url: str) -> bool:
    """Search results and page links are untrusted, so they can't reach internal hosts."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror:
        return False
    for *_, sockaddr in addresses:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            return False
    return True


def fetch_text(url: str) -> str | None:
    if not is_public_url(url):
        return None
    try:
        with httpx.Client(timeout=12, follow_redirects=False, headers={"User-Agent": "Redline/1.0"}) as client:
            current = url
            for _ in range(4):
                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        current = str(response.next_request.url) if response.next_request else ""
                        if not is_public_url(current):
                            return None
                        continue
                    response.raise_for_status()
                    if "html" not in response.headers.get("content-type", ""):
                        return None
                    body = b""
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > MAX_PAGE_BYTES:
                            break
                    text = trafilatura.extract(body.decode(response.encoding or "utf-8", errors="replace"))
                    return text[:MAX_PAGE_CHARS] if text else None
    except httpx.HTTPError as exc:
        logger.info("Could not fetch %s: %s", url, exc)
    return None
