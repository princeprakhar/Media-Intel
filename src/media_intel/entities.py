import re
import difflib
import spacy
from typing import List, Dict, Optional, Tuple, Set

HONORIFIC_RE = re.compile(r"^(Mr|Mrs|Ms|Dr|Prof|Sir|Madam|President|Senator|Rep)\.?\s+", re.I)
POSSESSIVE_RE = re.compile(r"[\u2019']s$")
HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{2,30})")

LABEL_MAP = {
    "PERSON": "PERSON", "ORG": "ORG", "GPE": "LOCATION",
    "LOC": "LOCATION", "NORP": "GROUP", "EVENT": "EVENT", "FAC": "LOCATION",
}

STOPWORD_TOPIC_HEADS = {"thing", "way", "time", "people", "year", "day"}

# Place names are exactly the category where high string similarity means
# nothing semantically: "South Africa" vs "South America" scores 0.88 on
# difflib's ratio (shared "South A" prefix + "rica" suffix) — right at our
# cutoff — and fuzzy-merging them is a real, observed failure in this
# project's own scraped data (see README). Fuzzy matching is disabled
# entirely for LOCATION, since the risk of a wrong merge outweighs the
# benefit of catching minor spelling variants for place names.
FUZZY_EXCLUDED_TYPES = {"LOCATION"}
FUZZY_CUTOFF = 0.90


def clean_name(text: str) -> str:
    t = text.strip().lstrip("@")
    t = HONORIFIC_RE.sub("", t)
    t = POSSESSIVE_RE.sub("", t)
    t = t.strip(" \t\n\"'\u2018\u2019\u201c\u201d.,;:()")
    t = re.sub(r"\s+", " ", t)
    return t


class EntityRegistry:
    def __init__(self, ignore_entities: Optional[Set[str]] = None):
        self.canonical_by_key: Dict[str, str] = {}
        self.type_by_canonical: Dict[str, str] = {}
        self.aliases_by_canonical: Dict[str, set] = {}
        self.surname_index: Dict[Tuple[str, str], str] = {}
        self.ignore_entities = ignore_entities or set()

    def load_from_db(self, rows):
        for canonical, etype, aliases in rows:
            self.type_by_canonical[canonical] = etype
            self.aliases_by_canonical.setdefault(canonical, set()).update(aliases or [])
            self.canonical_by_key[canonical.lower()] = canonical
            for a in (aliases or []):
                self.canonical_by_key[a.lower()] = canonical
            toks = canonical.split()
            if etype == "PERSON" and len(toks) > 1:
                self.surname_index[(etype, toks[-1].lower())] = canonical

    def seed_manual(self, manual_aliases: Dict[str, List[str]], manual_types: Dict[str, str]):
        for canonical, aliases in (manual_aliases or {}).items():
            self.canonical_by_key[canonical.lower()] = canonical
            self.aliases_by_canonical.setdefault(canonical, set()).update(aliases)
            for a in aliases:
                self.canonical_by_key[a.lower()] = canonical
        for canonical, etype in (manual_types or {}).items():
            self.type_by_canonical[canonical] = etype
            self.canonical_by_key.setdefault(canonical.lower(), canonical)

    def _fuzzy_match(self, cleaned: str, etype: str) -> Optional[str]:
        if etype in FUZZY_EXCLUDED_TYPES:
            return None
        candidates = [n for n, t in self.type_by_canonical.items() if t == etype]
        best = difflib.get_close_matches(cleaned, candidates, n=1, cutoff=FUZZY_CUTOFF)
        return best[0] if best else None

    def resolve(self, raw_text: str, etype: str) -> Optional[str]:
        cleaned = clean_name(raw_text)
        if len(cleaned) < 2:
            return None
        if cleaned.lower() in self.ignore_entities:
            return None
        key = cleaned.lower()

        if key in self.canonical_by_key:
            canonical = self.canonical_by_key[key]
            self.aliases_by_canonical.setdefault(canonical, set()).add(cleaned)
            if canonical not in self.type_by_canonical:
                self.type_by_canonical[canonical] = etype
            return canonical

        if etype == "PERSON":
            toks = cleaned.split()
            if len(toks) == 1:
                hit = self.surname_index.get((etype, toks[0].lower()))
                if hit:
                    self.canonical_by_key[key] = hit
                    self.aliases_by_canonical.setdefault(hit, set()).add(cleaned)
                    return hit

        fuzzy = self._fuzzy_match(cleaned, etype)
        if fuzzy:
            self.canonical_by_key[key] = fuzzy
            self.aliases_by_canonical.setdefault(fuzzy, set()).add(cleaned)
            return fuzzy

        self.canonical_by_key[key] = cleaned
        self.type_by_canonical[cleaned] = etype
        self.aliases_by_canonical.setdefault(cleaned, set()).add(cleaned)
        toks = cleaned.split()
        if etype == "PERSON" and len(toks) > 1:
            self.surname_index[(etype, toks[-1].lower())] = cleaned
        return cleaned

    def get_type(self, canonical: str) -> Optional[str]:
        return self.type_by_canonical.get(canonical)

    def get_aliases(self, canonical: str):
        return sorted(self.aliases_by_canonical.get(canonical, set()))

    def resolve_username(self, username: str) -> Optional[str]:
        if not username:
            return None
        return self.resolve(username, "PERSON")


SENTENCE_END_RE = re.compile(r'[.!?]["\')]*$')


def _ensure_line_boundaries(text: str) -> str:
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if not SENTENCE_END_RE.search(s):
            s = s + "."
        lines.append(s)
    return "\n".join(lines)


class EntityExtractor:
    def __init__(self, model_name: str, registry: EntityRegistry):
        self.nlp = spacy.load(model_name)
        self.registry = registry

    def process(self, text: str):
        text = _ensure_line_boundaries(text)
        doc = self.nlp(text)
        sent_entities = []

        for sent in doc.sents:
            ents_in_sent = []
            seen_roots = set()

            for ent in sent.ents:
                if ent.label_ not in LABEL_MAP:
                    continue
                etype = LABEL_MAP[ent.label_]
                canonical = self.registry.resolve(ent.text, etype)
                if not canonical:
                    continue
                ents_in_sent.append({
                    "canonical": canonical, "type": self.registry.get_type(canonical) or etype,
                    "root_token": ent.root, "span_text": ent.text,
                })
                seen_roots.add(ent.root.i)

            for m in HANDLE_RE.finditer(sent.text):
                handle = m.group(1)
                canonical = self.registry.resolve(handle, "PERSON")
                if canonical:
                    approx_token = sent[0]
                    for tok in sent:
                        if tok.text.lstrip("@") == handle:
                            approx_token = tok
                            break
                    ents_in_sent.append({
                        "canonical": canonical, "type": "PERSON",
                        "root_token": approx_token, "span_text": "@" + handle,
                    })

            for chunk in sent.noun_chunks:
                if chunk.root.i in seen_roots:
                    continue
                words = [t.text.lower() for t in chunk if not t.is_stop and t.is_alpha]
                if len(words) < 2:
                    continue
                if chunk.root.lemma_.lower() in STOPWORD_TOPIC_HEADS:
                    continue
                topic_text = " ".join(words)
                if len(topic_text) < 4:
                    continue
                canonical = self.registry.resolve(topic_text, "TOPIC")
                if not canonical:
                    continue
                ents_in_sent.append({
                    "canonical": canonical, "type": "TOPIC",
                    "root_token": chunk.root, "span_text": chunk.text,
                })

            sent_entities.append((sent, ents_in_sent))
        return sent_entities