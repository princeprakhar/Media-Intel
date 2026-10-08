import re
import json as _json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List

from .crawler import RawPage
from .threads import parse_hn_thread, parse_reddit_thread, CommentNode


AUTHOR_PATTERNS = [
    re.compile(r"\bBy\s+([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3})"),
    re.compile(r"\bWritten by\s+([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3})"),
    re.compile(r"\bposted by\s+([A-Za-z0-9_-]{2,30})", re.I),
]

DATE_PATTERNS = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"),
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})"),
]

NAV_NOISE_WORDS = {
    "subscribe", "sign in", "log in", "menu", "home", "sections",
    "advertisement", "cookie", "newsletter", "share", "skip to content",
}

MARKDOWN_LINK_RE = re.compile(r"\[([^\]]{1,80})\]\(([^)]+)\)")
HN_ITEM_RE = re.compile(r"news\.ycombinator\.com/item\?id=")
REDDIT_THREAD_RE = re.compile(r"reddit\.com/r/[^/]+/comments/")
REDDIT_JSON_URL_RE = re.compile(r"reddit\.com/r/[^/]+/(?:top/)?\.json", re.I)

# Many CMSes suffix every page's <title> with site/section chrome, e.g.
# Al Jazeera: "US Republicans... Poll | US Midterm Elections 2026 News | Al
# Jazeera". Feeding that chrome into NLP creates garbage TOPIC nodes like
# "poll midterm elections news al jazeera". Strip trailing "| segment"
# parts, bounded to a few iterations so genuine title content with a
# literal pipe isn't eaten.
TITLE_SITE_SUFFIX_RE = re.compile(r"\s*\|\s*[^|]{1,40}$")

# "Share" (and similar UI action-button labels) commonly precede a headline
# with no separating space once markdown link syntax is stripped (original
# source: "[Share](url)Harmanpreet Kaur: ..."). Strip the leading UI word
# only when immediately followed by a capital letter with no space, to
# avoid false positives on genuine words.
UI_ACTION_PREFIX_RE = re.compile(r"^(Share|Print|Email|Save|Bookmark)\s+(?=[A-Z][a-z])")


@dataclass
class NormalizedDocument:
    source_url: str
    source_type: str
    scraped_at: str
    title: Optional[str]
    body: Optional[str]
    author: Optional[str]
    published_at: Optional[str]
    content_hash: str
    op_author: Optional[str] = None
    comments: List[CommentNode] = field(default_factory=list)


def _clean_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return title
    cleaned = title
    for _ in range(3):
        new = TITLE_SITE_SUFFIX_RE.sub("", cleaned).strip()
        if new == cleaned or not new:
            break
        cleaned = new
    return cleaned or title


def _try_parse_reddit_json(raw_body: str) -> Optional[str]:
    try:
        data = _json.loads(raw_body)
        if isinstance(data, list):
            children = data[0]["data"]["children"]
        else:
            children = data["data"]["children"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None

    lines = []
    for child in children:
        post = child.get("data", {})
        title = post.get("title", "")
        selftext = post.get("selftext", "")
        author = post.get("author", "")
        if not title:
            continue
        line = f"{title}. Posted by {author}."
        if selftext:
            line += f" {selftext}"
        lines.append(line.strip())
    return "\n".join(lines) if lines else None


def _clean_body(markdown: str) -> str:
    kept = []
    for line in markdown.splitlines():
        s = line.strip()
        if not s:
            continue

        if len(MARKDOWN_LINK_RE.findall(s)) >= 2:
            continue

        s = MARKDOWN_LINK_RE.sub(r"\1", s)
        s = UI_ACTION_PREFIX_RE.sub("", s)

        if s.startswith("!") and len(s) < 40:
            continue

        low = s.lower()
        if len(s) < 25 and any(w in low for w in NAV_NOISE_WORDS):
            continue
        if s.startswith("![") or s.startswith("[!["):
            continue
        kept.append(s)
    return "\n".join(kept)


def _find_author(text: str) -> Optional[str]:
    for pat in AUTHOR_PATTERNS:
        m = pat.search(text[:2000])
        if m:
            return m.group(1).strip()
    return None


def _find_published(text: str) -> Optional[str]:
    for pat in DATE_PATTERNS:
        m = pat.search(text[:3000])
        if m:
            return m.group(1)
    return None


def _maybe_parse_thread(raw: RawPage, max_comments: int):
    html = raw.html or ""
    if HN_ITEM_RE.search(raw.url):
        op_author, comments = parse_hn_thread(html)
        return op_author, comments[:max_comments]
    if REDDIT_THREAD_RE.search(raw.url):
        op_author, comments = parse_reddit_thread(html)
        return op_author, comments[:max_comments]
    return None, []


def normalize(raw: RawPage, max_comments_per_thread: int = 40) -> NormalizedDocument:
    reddit_json_body = None
    if REDDIT_JSON_URL_RE.search(raw.url):
        reddit_json_body = _try_parse_reddit_json(raw.html or raw.markdown or "")

    if reddit_json_body:
        body = reddit_json_body
    else:
        body = _clean_body(raw.markdown or "")

    title = _clean_title(raw.title)
    if not title:
        for line in body.splitlines():
            if line.strip():
                title = line.strip("# ").strip()
                break

    author = _find_author(raw.html or body)
    published = _find_published(raw.html or body)
    scraped_at = datetime.now(timezone.utc).isoformat()
    content_hash = hashlib.sha256((raw.url + (body or "")[:500]).encode("utf-8")).hexdigest()

    op_author, comments = _maybe_parse_thread(raw, max_comments_per_thread)

    return NormalizedDocument(
        source_url=raw.url,
        source_type=raw.source_type,
        scraped_at=scraped_at,
        title=title or None,
        body=body or None,
        author=author,
        published_at=published,
        content_hash=content_hash,
        op_author=op_author,
        comments=comments,
    )