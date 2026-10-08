import json
from itertools import combinations
from typing import Dict, Optional, List


def _row_to_node(row):
    return {
        "id": row[0], "name": row[1], "type": row[2],
        "aliases": json.loads(row[3]) if row[3] else [],
        "first_seen": row[4], "last_seen": row[5], "mention_count": row[6],
    }


def find_node(conn, name: str):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, canonical_name, entity_type, aliases, first_seen, last_seen, mention_count "
        "FROM nodes WHERE lower(canonical_name) = lower(?)", (name,),
    )
    row = cur.fetchone()
    if row:
        return _row_to_node(row)

    cur.execute("SELECT id, canonical_name, entity_type, aliases, first_seen, last_seen, mention_count FROM nodes")
    for row in cur.fetchall():
        aliases = json.loads(row[3]) if row[3] else []
        if any(a.lower() == name.lower() for a in aliases):
            return _row_to_node(row)

    cur.execute(
        "SELECT id, canonical_name, entity_type, aliases, first_seen, last_seen, mention_count "
        "FROM nodes WHERE lower(canonical_name) LIKE ?", (f"%{name.lower()}%",),
    )
    row = cur.fetchone()
    return _row_to_node(row) if row else None


def _neighbors(conn, node_id: int):
    cur = conn.cursor()
    cur.execute("""
        SELECT e.id, e.source_node_id, e.target_node_id, e.relation_type, e.weight,
               e.first_seen, e.last_seen,
               n1.canonical_name, n1.entity_type, n2.canonical_name, n2.entity_type
        FROM edges e
        JOIN nodes n1 ON n1.id = e.source_node_id
        JOIN nodes n2 ON n2.id = e.target_node_id
        WHERE e.source_node_id = ? OR e.target_node_id = ?
    """, (node_id, node_id))
    return cur.fetchall()


def entity_network(conn, name: str, depth2: bool = True,
                    exclude_relations: Optional[List[str]] = None,
                    min_weight: int = 0):
    exclude_relations = set(exclude_relations or [])
    center = find_node(conn, name)
    if not center:
        return None

    nodes = {center["id"]: {"id": center["id"], "name": center["name"], "type": center["type"]}}
    edges, edge_ids_seen, depth1_ids = [], set(), set()

    def _passes(rel, weight):
        return rel not in exclude_relations and weight >= min_weight

    for row in _neighbors(conn, center["id"]):
        (eid, s_id, t_id, rel, weight, fseen, lseen, s_name, s_type, t_name, t_type) = row
        if eid in edge_ids_seen or not _passes(rel, weight):
            continue
        edge_ids_seen.add(eid)
        nodes.setdefault(s_id, {"id": s_id, "name": s_name, "type": s_type})
        nodes.setdefault(t_id, {"id": t_id, "name": t_name, "type": t_type})
        edges.append({"id": eid, "source": s_name, "target": t_name, "relation": rel,
                       "weight": weight, "first_seen": fseen, "last_seen": lseen, "depth": 1})
        depth1_ids.add(t_id if s_id == center["id"] else s_id)

    if depth2:
        for nid in depth1_ids:
            for row in _neighbors(conn, nid):
                (eid, s_id, t_id, rel, weight, fseen, lseen, s_name, s_type, t_name, t_type) = row
                if eid in edge_ids_seen or not _passes(rel, weight):
                    continue
                edge_ids_seen.add(eid)
                nodes.setdefault(s_id, {"id": s_id, "name": s_name, "type": s_type})
                nodes.setdefault(t_id, {"id": t_id, "name": t_name, "type": t_type})
                edges.append({"id": eid, "source": s_name, "target": t_name, "relation": rel,
                              "weight": weight, "first_seen": fseen, "last_seen": lseen, "depth": 2})

    return {"center": center["name"], "nodes": list(nodes.values()), "edges": edges}


def edge_sources(conn, edge_id: int):
    """Answers 'according to what, and when?' for a specific edge."""
    cur = conn.cursor()
    cur.execute("""
        SELECT es.snippet, es.extracted_at, d.source_url, d.source_type, d.title
        FROM edge_sources es
        JOIN documents d ON d.id = es.document_id
        WHERE es.edge_id = ?
        ORDER BY es.extracted_at DESC
    """, (edge_id,))
    return [
        {"snippet": s, "extracted_at": t, "source_url": u, "source_type": st, "title": title}
        for s, t, u, st, title in cur.fetchall()
    ]


def emerging_connections(conn, since: str, min_absolute: int = 2, growth_ratio: float = 1.5, limit: int = 50):
    cur = conn.cursor()
    cur.execute("""
        SELECT e.id, n1.canonical_name, n2.canonical_name, e.relation_type, e.weight,
               e.first_seen, e.last_seen
        FROM edges e
        JOIN nodes n1 ON n1.id = e.source_node_id
        JOIN nodes n2 ON n2.id = e.target_node_id
    """)
    edge_rows = cur.fetchall()
    results = []
    for (eid, s_name, t_name, rel, weight, fseen, lseen) in edge_rows:
        cur.execute("SELECT COUNT(*) FROM edge_sources WHERE edge_id=? AND extracted_at < ?", (eid, since))
        before = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM edge_sources WHERE edge_id=? AND extracted_at >= ?", (eid, since))
        after = cur.fetchone()[0]
        if after == 0:
            continue
        is_new = before == 0
        grown = (not is_new) and (after >= before * growth_ratio) and (after - before >= min_absolute)
        if is_new or grown:
            results.append({
                "id": eid, "source": s_name, "target": t_name, "relation": rel, "weight": weight,
                "mentions_before": before, "mentions_after": after,
                "status": "new" if is_new else "grown",
                "first_seen": fseen, "last_seen": lseen,
            })
    results.sort(key=lambda r: (r["status"] == "new", r["mentions_after"] - r["mentions_before"]), reverse=True)
    return results[:limit]


def central_entities(conn, limit: int = 20, entity_type: Optional[str] = None,
                      exclude_relations: Optional[List[str]] = None):
    exclude_relations = set(exclude_relations or [])
    MAX_NEIGHBORS_FOR_BRIDGE = 250
    cur = conn.cursor()
    cur.execute("SELECT id, canonical_name, entity_type FROM nodes")
    all_nodes = {r[0]: {"name": r[1], "type": r[2]} for r in cur.fetchall()}

    cur.execute("SELECT source_node_id, target_node_id, relation_type FROM edges")
    adjacency: Dict[int, Dict[int, set]] = {}
    direct_pairs = set()
    for s, t, rel in cur.fetchall():
        if rel in exclude_relations:
            continue
        adjacency.setdefault(s, {}).setdefault(t, set()).add(rel)
        adjacency.setdefault(t, {}).setdefault(s, set()).add(rel)
        direct_pairs.add(frozenset((s, t)))

    scored = []
    for node_id, info in all_nodes.items():
        if entity_type and info["type"] != entity_type:
            continue
        neighbors = adjacency.get(node_id, {})
        degree = len(neighbors)
        rel_types = set()
        for rels in neighbors.values():
            rel_types.update(rels)

        neighbor_ids = list(neighbors.keys())
        bridge_pairs, capped = 0, False
        if len(neighbor_ids) <= MAX_NEIGHBORS_FOR_BRIDGE:
            for a, b in combinations(neighbor_ids, 2):
                if frozenset((a, b)) not in direct_pairs:
                    bridge_pairs += 1
        else:
            capped = True

        score = degree * 1.0 + len(rel_types) * 0.5 + bridge_pairs * 2.0
        scored.append({
            "name": info["name"], "type": info["type"], "degree": degree,
            "distinct_relation_types": len(rel_types), "bridge_pairs": bridge_pairs,
            "bridge_capped": capped, "score": round(score, 2),
        })
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]