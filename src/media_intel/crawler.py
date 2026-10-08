import asyncio
import hashlib
import json
import logging
import os
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from typing import List, Optional
from urllib.parse import urlparse, urljoin

from .config import AppConfig

logger = logging.getLogger(__name__)

RAW_CACHE_DIR = "data/raw"

BLOCKED_PATH_PATTERNS = re.compile(
    r"(/login|/logout|/signup|/vote\?|/submit$|/reply\?|/hide\?|/save\?|"
    r"goto=|/search|/user\?)", re.I
)
ARTICLE_LIKE_RE = re.compile(r"/\d{4}/\d{1,2}/\d{1,2}/")

ASSET_EXT_RE = re.compile(
    r"\.(pdf|jpg|jpeg|png|gif|svg|css|js|ico|woff2?|mp4|webm)(\?|$)", re.I,
)
FEED_EXT_RE = re.compile(r"\.xml(\?|$)", re.I)
FEED_ITEM_LINK_RE = re.compile(r"<(?:link|loc)>\s*(https?://[^<\s]+)\s*</(?:link|loc)>")

FEED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class RawPage:
    url: str
    source_type: str
    html: str
    markdown: str
    title: Optional[str]
    depth: int


def domain_allowed(url: str, whitelist: List[str]) -> bool:
    if not whitelist:
        return True
    netloc = urlparse(url).netloc.lower().split(":")[0]
    for d in whitelist:
        d = d.lower()
        if netloc == d or netloc.endswith("." + d):
            return True
    return False


def _looks_like_listing_page(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return True
    segments = [s for s in path.split("/") if s]
    haystack = path + ("?" + parsed.query if parsed.query else "")
    has_digit = any(ch.isdigit() for ch in haystack)
    return len(segments) <= 2 and not has_digit


def is_crawlable_content_link(url: str) -> bool:
    if BLOCKED_PATH_PATTERNS.search(url):
        return False
    if ASSET_EXT_RE.search(urlparse(url).path):
        return False
    if FEED_EXT_RE.search(urlparse(url).path):
        return True
    if ARTICLE_LIKE_RE.search(url):
        return True
    if _looks_like_listing_page(url):
        return False
    return True


def is_feed_url(url: str) -> bool:
    return bool(FEED_EXT_RE.search(urlparse(url).path))


def fetch_feed_xml_raw(url: str, timeout: float = 10.0) -> str:
    """
    Fetches XML feeds via a plain HTTP GET instead of crawl4ai's browser.

    Why: crawl4ai renders every page through headless Chromium, and
    Chromium does NOT hand back raw XML bytes for .xml files — it renders
    its own built-in XML tree viewer, which replaces the original
    "<link>https://...</link>" text with syntax-highlighting markup. Our
    link-extraction regex was matching against that destroyed structure
    and correctly found nothing. A feed is a machine-readable data file,
    not a rendered page, so fetching it directly (no JS, no DOM) is the
    technically correct tool here, not a workaround.
    """
    req = urllib.request.Request(url, headers={"User-Agent": FEED_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.warning("Direct feed fetch failed for %s: %s", url, e)
        return ""


def extract_feed_article_links(xml_text: str) -> List[str]:
    return FEED_ITEM_LINK_RE.findall(xml_text or "")


def _extract_markdown(result) -> str:
    md = getattr(result, "markdown", None)
    if md is None:
        val = getattr(result, "cleaned_html", "") or getattr(result, "html", "") or ""
        return val if isinstance(val, str) else str(val)
    if isinstance(md, str):
        return md
    for attr in ("raw_markdown", "fit_markdown", "markdown_with_citations"):
        val = getattr(md, attr, None)
        if val:
            return val if isinstance(val, str) else str(val)
    return str(md)


def _extract_links(result, base_url: str) -> List[str]:
    links = []
    raw_links = getattr(result, "links", None)
    if raw_links:
        for bucket in ("internal", "external"):
            for item in raw_links.get(bucket, []) or []:
                href = item.get("href") if isinstance(item, dict) else item
                if href:
                    links.append(urljoin(base_url, href))
    if not links:
        html = getattr(result, "html", "") or ""
        if html:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "lxml")
                for a in soup.find_all("a", href=True):
                    links.append(urljoin(base_url, a["href"]))
            except Exception as e:
                logger.warning("fallback link extraction failed: %s", e)
    return links


def _cache_path(url: str) -> str:
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    return os.path.join(RAW_CACHE_DIR, f"{h}.json")


def save_raw_page(page: RawPage):
    try:
        os.makedirs(RAW_CACHE_DIR, exist_ok=True)
        with open(_cache_path(page.url), "w") as f:
            json.dump(asdict(page), f, default=str)
    except Exception as e:
        logger.warning("Failed to cache raw page %s: %s", page.url, e)


def load_cached_pages() -> List[RawPage]:
    pages = []
    if not os.path.isdir(RAW_CACHE_DIR):
        return pages
    for fname in os.listdir(RAW_CACHE_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(RAW_CACHE_DIR, fname)
        try:
            with open(path) as f:
                d = json.load(f)
            pages.append(RawPage(**d))
        except Exception as e:
            logger.warning("Skipping corrupt cache file %s: %s", path, e)
    return pages


async def crawl_all(config: AppConfig) -> List[RawPage]:
    from crawl4ai import AsyncWebCrawler

    for seed in config.seeds:
        if not domain_allowed(seed.url, config.domain_whitelist):
            logger.warning(
                "Seed %s (%s) is NOT in domain_whitelist %s — it will be skipped entirely.",
                seed.url, seed.source_type, config.domain_whitelist,
            )

    pages: List[RawPage] = []
    visited = set()

    async with AsyncWebCrawler() as crawler:
        for seed in config.seeds:
            queue = [(seed.url, 0)]
            pages_from_seed = 0
            while queue and pages_from_seed < config.max_pages_per_seed:
                url, depth = queue.pop(0)
                if url in visited or not domain_allowed(url, config.domain_whitelist):
                    continue
                visited.add(url)

                # Feeds: fetch directly, never through the browser (see
                # fetch_feed_xml_raw docstring for why). This also skips
                # the usual per-request delay/browser overhead entirely.
                if is_feed_url(url):
                    xml_text = await asyncio.to_thread(fetch_feed_xml_raw, url)
                    feed_links = extract_feed_article_links(xml_text)
                    logger.info("Feed %s yielded %d article links", url, len(feed_links))
                    pages_from_seed += 1
                    for link in feed_links:
                        if (link not in visited
                                and domain_allowed(link, config.domain_whitelist)
                                and is_crawlable_content_link(link)):
                            queue.append((link, depth))
                    continue

                try:
                    result = await crawler.arun(url=url)
                except Exception as e:
                    logger.warning("Failed to crawl %s: %s", url, e)
                    continue
                if not getattr(result, "success", True):
                    logger.warning("crawl4ai reported failure for %s", url)
                    continue

                html = getattr(result, "html", "") or ""
                markdown = _extract_markdown(result)
                metadata = getattr(result, "metadata", None) or {}
                title = metadata.get("title") if isinstance(metadata, dict) else None

                page = RawPage(
                    url=url, source_type=seed.source_type, html=html,
                    markdown=markdown, title=title, depth=depth,
                )
                pages.append(page)
                if config.cache_raw_pages:
                    save_raw_page(page)
                pages_from_seed += 1

                if depth < config.max_depth:
                    candidate_links = _extract_links(result, url)
                    for link in candidate_links:
                        if (link not in visited
                                and domain_allowed(link, config.domain_whitelist)
                                and is_crawlable_content_link(link)):
                            queue.append((link, depth + 1))

                await asyncio.sleep(config.request_delay_seconds)
    return pages