import yaml
from dataclasses import dataclass, field
from typing import List, Dict, Any


@dataclass
class SeedConfig:
    url: str
    source_type: str


@dataclass
class AppConfig:
    seeds: List[SeedConfig]
    domain_whitelist: List[str]
    max_depth: int
    max_pages_per_seed: int
    request_delay_seconds: float
    cache_raw_pages: bool
    db_path: str
    spacy_model: str
    min_entity_length: int
    max_comments_per_thread: int
    ignore_entities: List[str]
    manual_aliases: Dict[str, List[str]]
    manual_types: Dict[str, str]
    raw: Dict[str, Any] = field(default_factory=dict)


def load_config(path: str) -> AppConfig:
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    seeds = [
        SeedConfig(url=s["url"], source_type=s.get("source_type", "news"))
        for s in raw["seeds"]
    ]
    crawl = raw.get("crawl", {})
    storage = raw.get("storage", {})
    extraction = raw.get("extraction", {})

    return AppConfig(
        seeds=seeds,
        domain_whitelist=crawl.get("domain_whitelist", []),
        max_depth=crawl.get("max_depth", 1),
        max_pages_per_seed=crawl.get("max_pages_per_seed", 10),
        request_delay_seconds=crawl.get("request_delay_seconds", 1.0),
        cache_raw_pages=crawl.get("cache_raw_pages", True),
        db_path=storage.get("db_path", "data/media_intel.db"),
        spacy_model=extraction.get("spacy_model", "en_core_web_sm"),
        min_entity_length=extraction.get("min_entity_length", 2),
        max_comments_per_thread=extraction.get("max_comments_per_thread", 40),
        ignore_entities=[s.lower() for s in extraction.get("ignore_entities", [])],
        manual_aliases=extraction.get("manual_aliases", {}),
        manual_types=extraction.get("manual_types", {}),
        raw=raw,
    )