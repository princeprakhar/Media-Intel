import argparse
from media_intel.pipeline import run


def cli():
    parser = argparse.ArgumentParser(description="Media intelligence pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_all = sub.add_parser("all", help="crawl + normalize + extract (default)")
    p_all.add_argument("--config", default="config.yaml")

    p_process = sub.add_parser("process", help="normalize + extract from cached raw pages, no crawl")
    p_process.add_argument("--config", default="config.yaml")

    p_stats = sub.add_parser("stats", help="print quick graph stats")
    p_stats.add_argument("--config", default="config.yaml")

    p_sample = sub.add_parser("sample", help="eyeball N random edges with evidence")
    p_sample.add_argument("--config", default="config.yaml")
    p_sample.add_argument("--relation", default=None)
    p_sample.add_argument("--n", type=int, default=15)

    args = parser.parse_args()

    if args.command == "all":
        run(args.config, skip_crawl=False)
    elif args.command == "process":
        run(args.config, skip_crawl=True)
    elif args.command == "stats":
        _print_stats(args.config)
    elif args.command == "sample":
        _print_sample(args.config, args.relation, args.n)


def _print_stats(config_path):
    import sqlite3
    from media_intel.config import load_config
    cfg = load_config(config_path)
    conn = sqlite3.connect(cfg.db_path)
    cur = conn.cursor()
    cur.execute("SELECT source_type, COUNT(*) FROM documents GROUP BY source_type")
    print("Documents by source_type:")
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]}")
    cur.execute("SELECT COUNT(*) FROM nodes")
    print(f"Total nodes: {cur.fetchone()[0]}")
    cur.execute("SELECT relation_type, COUNT(*) FROM edges GROUP BY relation_type")
    print("Edges by relation_type:")
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]}")
    cur.execute("SELECT canonical_name, entity_type, mention_count FROM nodes ORDER BY mention_count DESC LIMIT 15")
    print("Top 15 nodes by mention_count:")
    for row in cur.fetchall():
        print(f"  {row[0]} ({row[1]}): {row[2]}")
    conn.close()


def _print_sample(config_path, relation, n):
    import sqlite3, random
    from media_intel.config import load_config
    cfg = load_config(config_path)
    conn = sqlite3.connect(cfg.db_path)
    cur = conn.cursor()
    q = """SELECT es.snippet, n1.canonical_name, e.relation_type, n2.canonical_name, d.source_url
           FROM edge_sources es
           JOIN edges e ON e.id = es.edge_id
           JOIN nodes n1 ON n1.id = e.source_node_id
           JOIN nodes n2 ON n2.id = e.target_node_id
           JOIN documents d ON d.id = es.document_id"""
    params = []
    if relation:
        q += " WHERE e.relation_type = ?"
        params.append(relation)
    cur.execute(q, params)
    rows = cur.fetchall()
    random.shuffle(rows)
    for snippet, src, rel, tgt, url in rows[:n]:
        print(f"[{rel}] {src} -> {tgt}")
        print(f"  evidence: {snippet}")
        print(f"  source:   {url}\n")
    conn.close()


if __name__ == "__main__":
    cli()