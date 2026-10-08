import os
import sqlite3
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Query

from .config import load_config
from . import analysis

CONFIG_PATH = os.environ.get("MEDIA_INTEL_CONFIG", "config.yaml")
_config = load_config(CONFIG_PATH)

app = FastAPI(title="Media Intelligence Graph API")


def get_conn():
    return sqlite3.connect(_config.db_path, check_same_thread=False)


@app.get("/health")
def health():
    return {"status": "ok", "db_path": _config.db_path}


@app.get("/entity/{name}/network")
def entity_network(
    name: str,
    depth2: bool = Query(True),
    exclude_relations: Optional[str] = Query(None, description="comma-separated relation types to exclude"),
    min_weight: int = Query(0, ge=0),
):
    conn = get_conn()
    excl = exclude_relations.split(",") if exclude_relations else []
    try:
        result = analysis.entity_network(conn, name, depth2=depth2, exclude_relations=excl, min_weight=min_weight)
    finally:
        conn.close()
    if result is None:
        raise HTTPException(status_code=404, detail=f"Entity '{name}' not found")
    return result


@app.get("/connections/new")
def connections_new(since: str, min_absolute: int = 2, growth_ratio: float = 1.5, limit: int = 50):
    conn = get_conn()
    try:
        result = analysis.emerging_connections(conn, since, min_absolute, growth_ratio, limit)
    finally:
        conn.close()
    return {"since": since, "count": len(result), "connections": result}


@app.get("/entities/central")
def entities_central(
    limit: int = 20,
    entity_type: Optional[str] = Query(None),
    exclude_relations: Optional[str] = Query(None),
):
    conn = get_conn()
    excl = exclude_relations.split(",") if exclude_relations else []
    try:
        result = analysis.central_entities(conn, limit, entity_type=entity_type, exclude_relations=excl)
    finally:
        conn.close()
    return {"count": len(result), "entities": result}


@app.get("/edges/{edge_id}/sources")
def get_edge_sources(edge_id: int):
    conn = get_conn()
    try:
        result = analysis.edge_sources(conn, edge_id)
    finally:
        conn.close()
    if not result:
        raise HTTPException(status_code=404, detail=f"No sources found for edge {edge_id}")
    return {"edge_id": edge_id, "count": len(result), "sources": result}