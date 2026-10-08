import sqlite3
import json
import os
from typing import List

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT UNIQUE,
    source_type TEXT,
    scraped_at TEXT,
    title TEXT,
    body TEXT,
    author TEXT,
    published_at TEXT,
    content_hash TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT UNIQUE,
    entity_type TEXT,
    aliases TEXT,
    type_votes TEXT DEFAULT '{}',
    first_seen TEXT,
    last_seen TEXT,
    mention_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node_id INTEGER NOT NULL,
    target_node_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,
    weight INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    UNIQUE(source_node_id, target_node_id, relation_type),
    FOREIGN KEY(source_node_id) REFERENCES nodes(id),
    FOREIGN KEY(target_node_id) REFERENCES nodes(id)
);

CREATE TABLE IF NOT EXISTS edge_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    snippet TEXT,
    extracted_at TEXT,
    FOREIGN KEY(edge_id) REFERENCES edges(id),
    FOREIGN KEY(document_id) REFERENCES documents(id)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_node_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_node_id);
CREATE INDEX IF NOT EXISTS idx_edge_sources_edge ON edge_sources(edge_id);
CREATE INDEX IF NOT EXISTS idx_edge_sources_time ON edge_sources(extracted_at);
"""

UNDIRECTED_RELATIONS = {"mentioned_with"}


class GraphStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def upsert_document(self, doc) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM documents WHERE source_url = ?", (doc.source_url,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            """INSERT INTO documents (source_url, source_type, scraped_at, title, body,
               author, published_at, content_hash) VALUES (?,?,?,?,?,?,?,?)""",
            (doc.source_url, doc.source_type, doc.scraped_at, doc.title, doc.body,
             doc.author, doc.published_at, doc.content_hash),
        )
        self.conn.commit()
        return cur.lastrowid

    def content_hash_seen(self, content_hash: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM documents WHERE content_hash = ? LIMIT 1", (content_hash,))
        return cur.fetchone() is not None

    def load_entity_rows(self):
        cur = self.conn.cursor()
        cur.execute("SELECT canonical_name, entity_type, aliases FROM nodes")
        return [(name, etype, json.loads(a) if a else []) for name, etype, a in cur.fetchall()]

    def get_or_create_node(self, canonical_name: str, entity_type: str, aliases: List[str], ts: str) -> int:
        """
        Type is decided by majority vote across all mentions seen so far
        (type_votes), not frozen at first sight. This self-corrects a single
        bad NER tag early in a node's life — e.g. if "OpenAI" is first
        mistagged PERSON, a later ORG-tagged mention can still win the vote.
        manual_types (seeded into the registry before any crawling) always
        wins because the registry pins get_type() to the manual value, so
        every mention of that canonical arrives with the same forced type
        and the vote is unanimous.
        """
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, mention_count, aliases, type_votes FROM nodes WHERE canonical_name = ?",
            (canonical_name,),
        )
        row = cur.fetchone()
        if row:
            node_id, mention_count, aliases_json, votes_json = row
            existing = set(json.loads(aliases_json) if aliases_json else [])
            existing.update(aliases)
            votes = json.loads(votes_json) if votes_json else {}
            votes[entity_type] = votes.get(entity_type, 0) + 1
            winning_type = max(votes.items(), key=lambda kv: kv[1])[0]
            cur.execute(
                """UPDATE nodes SET mention_count = ?, last_seen = ?, aliases = ?,
                   type_votes = ?, entity_type = ? WHERE id = ?""",
                (mention_count + 1, ts, json.dumps(sorted(existing)),
                 json.dumps(votes), winning_type, node_id),
            )
            self.conn.commit()
            return node_id

        votes = {entity_type: 1}
        cur.execute(
            """INSERT INTO nodes (canonical_name, entity_type, aliases, type_votes,
               first_seen, last_seen, mention_count) VALUES (?,?,?,?,?,?,1)""",
            (canonical_name, entity_type, json.dumps(sorted(set(aliases))),
             json.dumps(votes), ts, ts),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_edge(self, relation_type: str, source_node_id: int, target_node_id: int,
                 ts: str, document_id: int, snippet: str) -> int:
        if relation_type in UNDIRECTED_RELATIONS and source_node_id > target_node_id:
            source_node_id, target_node_id = target_node_id, source_node_id
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, weight FROM edges WHERE source_node_id=? AND target_node_id=? AND relation_type=?",
            (source_node_id, target_node_id, relation_type),
        )
        row = cur.fetchone()
        if row:
            edge_id, weight = row
            cur.execute("UPDATE edges SET weight = ?, last_seen = ? WHERE id = ?", (weight + 1, ts, edge_id))
        else:
            cur.execute(
                """INSERT INTO edges (source_node_id, target_node_id, relation_type, weight, first_seen, last_seen)
                   VALUES (?,?,?,1,?,?)""",
                (source_node_id, target_node_id, relation_type, ts, ts),
            )
            edge_id = cur.lastrowid
        cur.execute(
            "INSERT INTO edge_sources (edge_id, document_id, snippet, extracted_at) VALUES (?,?,?,?)",
            (edge_id, document_id, snippet[:500], ts),
        )
        self.conn.commit()
        return edge_id