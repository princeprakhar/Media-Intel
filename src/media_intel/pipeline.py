import asyncio
import logging
from datetime import datetime, timezone
from collections import defaultdict

from .config import load_config
from .crawler import crawl_all, save_raw_page, load_cached_pages
from .normalizer import normalize
from .storage import GraphStore
from .entities import EntityRegistry, EntityExtractor
from .relations import extract_sentence_relations

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _strip_cross_document_boilerplate(documents, min_docs=3, frequency_threshold=0.6):
    """If an exact line of text appears in >= frequency_threshold of
    documents sharing a source_type, treat it as template chrome (nav bars,
    footers) rather than article content. Scoped per source_type since
    boilerplate on one site's template has no bearing on another site."""
    lines_by_type = defaultdict(lambda: defaultdict(int))
    doc_count_by_type = defaultdict(int)

    for doc in documents:
        if not doc.body:
            continue
        doc_count_by_type[doc.source_type] += 1
        for line in set(l.strip() for l in doc.body.splitlines() if l.strip()):
            lines_by_type[doc.source_type][line] += 1

    boilerplate = defaultdict(set)
    for source_type, counts in lines_by_type.items():
        total = doc_count_by_type[source_type]
        if total < min_docs:
            continue
        for line, count in counts.items():
            if count / total >= frequency_threshold:
                boilerplate[source_type].add(line)
        if boilerplate[source_type]:
            logger.info(
                "Stripped %d boilerplate lines from source_type=%s (seen in >=%.0f%% of %d docs)",
                len(boilerplate[source_type]), source_type, frequency_threshold * 100, total,
            )

    for doc in documents:
        if not doc.body:
            continue
        bp = boilerplate.get(doc.source_type)
        if not bp:
            continue
        doc.body = "\n".join(line for line in doc.body.splitlines() if line.strip() not in bp)
    return documents


def run(config_path: str, skip_crawl: bool = False):
    config = load_config(config_path)
    store = GraphStore(config.db_path)

    registry = EntityRegistry(ignore_entities=set(config.ignore_entities))
    registry.load_from_db(store.load_entity_rows())
    registry.seed_manual(config.manual_aliases, config.manual_types)
    extractor = EntityExtractor(config.spacy_model, registry)

    if skip_crawl:
        raw_pages = load_cached_pages()
        logger.info("Loaded %d cached raw pages (skipped crawl)", len(raw_pages))
    else:
        logger.info("Starting crawl for %d seeds", len(config.seeds))
        raw_pages = asyncio.run(crawl_all(config))
        logger.info("Crawled %d pages", len(raw_pages))

    documents = []
    for raw in raw_pages:
        doc = normalize(raw, max_comments_per_thread=config.max_comments_per_thread)
        if not doc.body or len(doc.body) < 50:
            logger.info("Skipping near-empty document: %s", doc.source_url)
            continue
        documents.append(doc)

    documents = _strip_cross_document_boilerplate(documents)

    seen_hashes_this_run = set()

    for doc in documents:
        if doc.content_hash in seen_hashes_this_run or store.content_hash_seen(doc.content_hash):
            logger.info("Skipping duplicate content (same hash as earlier doc): %s", doc.source_url)
            store.upsert_document(doc)
            continue
        seen_hashes_this_run.add(doc.content_hash)

        doc_id = store.upsert_document(doc)
        ts = datetime.now(timezone.utc).isoformat()
        node_id_cache = {}

        def ensure_node(canonical, etype):
            if canonical in node_id_cache:
                return node_id_cache[canonical]
            aliases = registry.get_aliases(canonical)
            node_id = store.get_or_create_node(canonical, etype, aliases, ts)
            node_id_cache[canonical] = node_id
            return node_id

        edge_count = 0

        if doc.comments:
            text = (doc.title or "")[:20000]
        else:
            text = ((doc.title or "") + ".\n" + doc.body)[:20000]

        for sent, ents in extractor.process(text):
            if len(ents) < 2:
                for e in ents:
                    ensure_node(e["canonical"], e["type"])
                continue
            for src, tgt, rel, snippet in extract_sentence_relations(sent, ents):
                src_type = registry.get_type(src)
                tgt_type = registry.get_type(tgt)
                src_id = ensure_node(src, src_type)
                tgt_id = ensure_node(tgt, tgt_type)
                store.add_edge(rel, src_id, tgt_id, ts, doc_id, snippet)
                edge_count += 1

        # --- Structural reply-graph extraction (HN / Reddit threads) ---
        # High-confidence, ground-truth directed edges: who replied to whom.
        # No sentence parsing involved — the direction and relation type
        # come directly from DOM/markup structure, not inference.
        for comment in doc.comments:
            if not comment.author:
                continue
            child_canonical = registry.resolve_username(comment.author)
            if not child_canonical:
                continue
            child_id = ensure_node(child_canonical, "PERSON")

            parent_name = comment.parent_author or doc.op_author
            if parent_name:
                parent_canonical = registry.resolve_username(parent_name)
                if parent_canonical and parent_canonical != child_canonical:
                    parent_id = ensure_node(parent_canonical, "PERSON")
                    snippet = f"[comment by {comment.author}]: {comment.text[:400]}"
                    store.add_edge("responded_to", child_id, parent_id, ts, doc_id, snippet)
                    edge_count += 1

            # Bonus: also run NLP over the comment's own text so named
            # entities mentioned inside comments (not just the commenters
            # themselves) still make it into the graph.
            if comment.text and len(comment.text) > 40:
                comment_text = f"{comment.text}"[:4000]
                for sent, ents in extractor.process(comment_text):
                    if len(ents) < 2:
                        for e in ents:
                            ensure_node(e["canonical"], e["type"])
                        continue
                    for src, tgt, rel, snippet in extract_sentence_relations(sent, ents):
                        src_type = registry.get_type(src)
                        tgt_type = registry.get_type(tgt)
                        src_id = ensure_node(src, src_type)
                        tgt_id = ensure_node(tgt, tgt_type)
                        attributed_snippet = f"[comment by {comment.author}]: {snippet}"
                        store.add_edge(rel, src_id, tgt_id, ts, doc_id, attributed_snippet)
                        edge_count += 1

        logger.info("Doc %s -> %d edges (%d comments)", doc.source_url, edge_count, len(doc.comments))

    store.close()
    logger.info("Pipeline run complete.")