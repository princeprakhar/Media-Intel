"""
Structural extraction of comment-reply relationships from discussion/social
sources. This is deliberately separate from the NLP pipeline in relations.py:
who-replied-to-whom is ground truth sitting directly in the page's DOM
structure, not something that needs to be inferred from sentence grammar.
Treating it as a first-class extraction path gives us a small number of
very high-confidence `responded_to` edges to balance the much larger volume
of lower-confidence NLP-derived edges.

Known fragility: both parsers depend on each site's current HTML markup.
HN's `indent` attribute and Reddit's nested `div.comment`/`div.child`
structure are the specific things that would break first if either site
changes its template. This is documented, not hidden — see README.
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple
from bs4 import BeautifulSoup


@dataclass
class CommentNode:
    comment_id: str
    author: Optional[str]
    parent_author: Optional[str]   # None => this is a direct reply to the OP
    depth: int
    text: str


def parse_hn_thread(html: str) -> Tuple[Optional[str], List[CommentNode]]:
    """
    HN's comment markup has no explicit parent-id pointer. Parent is
    inferred from nesting depth via a stack: a comment's parent is the
    most recent prior comment whose depth is exactly one less. This is
    the standard technique for HN scraping and is correct as long as HN's
    `td.ind[indent]` attribute continues to hold a literal depth integer
    (not a pixel width) — true at time of writing.
    """
    soup = BeautifulSoup(html, "lxml")
    op_author = None
    op_tag = soup.select_one(".fatitem .hnuser")
    if op_tag:
        op_author = op_tag.get_text(strip=True)

    comments: List[CommentNode] = []
    stack = []  # list of (depth, author)

    for row in soup.select("tr.athing.comtr"):
        comment_id = row.get("id", "")
        ind_td = row.select_one("td.ind")
        depth = 0
        if ind_td and ind_td.get("indent"):
            try:
                depth = int(ind_td["indent"])
            except ValueError:
                depth = 0

        user_tag = row.select_one("a.hnuser")
        author = user_tag.get_text(strip=True) if user_tag else None
        text_tag = row.select_one(".commtext")
        text = text_tag.get_text(" ", strip=True) if text_tag else ""

        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent_author = stack[-1][1] if stack else op_author

        comments.append(CommentNode(comment_id, author, parent_author, depth, text))
        stack.append((depth, author))

    return op_author, comments


def parse_reddit_thread(html: str) -> Tuple[Optional[str], List[CommentNode]]:
    """
    old.reddit's comment DOM is properly nested (div.comment > div.child >
    div.comment ...), so depth/parent fall out of straightforward recursive
    traversal rather than needing inference like HN requires.
    """
    soup = BeautifulSoup(html, "lxml")
    op_author = None
    op_tag = soup.select_one("div.thing.link")
    if op_tag and op_tag.get("data-author"):
        op_author = op_tag["data-author"]

    comments: List[CommentNode] = []

    def walk(container, parent_author, depth):
        for node in container.find_all("div", class_="comment", recursive=False):
            author = node.get("data-author")
            comment_id = node.get("id", "")
            md = node.select_one(".entry .md")
            text = md.get_text(" ", strip=True) if md else ""
            comments.append(CommentNode(comment_id, author, parent_author, depth, text))
            child_container = node.find("div", class_="child", recursive=False)
            if child_container:
                walk(child_container, author, depth + 1)

    root = soup.select_one("div.commentarea div.sitetable")
    if root:
        walk(root, op_author, 0)

    return op_author, comments