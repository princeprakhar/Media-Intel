import logging

logger = logging.getLogger(__name__)

SPEECH_VERBS = {"say", "tell", "claim", "announce", "state", "report", "note", "add", "argue", "insist", "warn"}
ACCUSE_VERBS = {"accuse", "blame"}
RESPOND_VERBS = {"respond", "reply", "react"}
AFFILIATION_VERBS = {"join", "lead", "head", "run", "own", "found", "chair", "represent", "work"}

# Only these prepositions genuinely introduce subject matter for a speech
# verb ("said ABOUT X", "spoke REGARDING Y"). Location/time/comparison
# prepositions (before, at, in, against, during) attach incidental context
# to a quote, not the thing being quoted about — treating them the same
# way caused a single quote-attribution sentence to fan out into five
# spurious "quoted" edges (Healy -> Mumbai, Healy -> Australia, Healy ->
# the Wankhede Stadium, etc.) from one real quote about something else
# entirely. Observed directly in scraped data; see README.
QUOTE_TOPIC_PREPS = {"about", "regarding", "on", "over", "concerning"}

# Hard cap on how many distinct entities in a single "sentence" are
# eligible for the pairwise co-occurrence fallback. Without this, a dense
# line (a stats table, an image-caption entity list, a nav/trending-topics
# sidebar our line-boundary fix treats as one sentence) with N entities
# produces N*(N-1)/2 mentioned_with edges from a single line. Verb/appos
# relations are NOT capped here — only the quadratic fallback is, since
# those stay linear in entity count regardless of sentence density.
MAX_ENTITIES_FOR_COOCCURRENCE = 8


def _descendant_or_self(container_token, target_token) -> bool:
    if container_token.i == target_token.i:
        return True
    return target_token in container_token.subtree


def _entities_under(token, entity_list):
    return [e for e in entity_list if _descendant_or_self(token, e["root_token"])]


def classify_verb(lemma: str) -> str:
    if lemma in ACCUSE_VERBS:
        return "accused_of"
    if lemma in RESPOND_VERBS:
        return "responded_to"
    if lemma in SPEECH_VERBS:
        return "quoted"
    if lemma in AFFILIATION_VERBS:
        return "affiliated_with"
    return "mentioned_with"


def extract_sentence_relations(sent, entities_in_sent):
    relations = []
    connected = set()

    for token in sent:
        if token.pos_ == "VERB":
            verb_lemma = token.lemma_.lower()
            is_speech_verb = verb_lemma in SPEECH_VERBS
            subj_ents, obj_ents = [], []
            for child in token.children:
                if child.dep_ in ("nsubj", "nsubjpass"):
                    subj_ents.extend(_entities_under(child, entities_in_sent))
                elif child.dep_ in ("dobj", "attr", "oprd"):
                    obj_ents.extend(_entities_under(child, entities_in_sent))
                elif child.dep_ == "prep":
                    # For speech verbs specifically, only prepositions that
                    # genuinely introduce subject matter count as objects —
                    # everything else (location/time/comparison PPs) is
                    # skipped so a single quote sentence doesn't fan out to
                    # every incidental noun phrase in it.
                    if is_speech_verb and child.text.lower() not in QUOTE_TOPIC_PREPS:
                        continue
                    for gc in child.children:
                        if gc.dep_ == "pobj":
                            obj_ents.extend(_entities_under(gc, entities_in_sent))
            rel = classify_verb(verb_lemma)
            for s in subj_ents:
                for o in obj_ents:
                    if s["canonical"] == o["canonical"]:
                        continue
                    relations.append((s["canonical"], o["canonical"], rel, sent.text.strip()))
                    connected.add(frozenset((s["canonical"], o["canonical"])))

        if token.dep_ == "appos":
            head_ents = _entities_under(token.head, entities_in_sent)
            self_ents = _entities_under(token, entities_in_sent)
            for a in self_ents:
                for b in head_ents:
                    if a["canonical"] == b["canonical"]:
                        continue
                    relations.append((a["canonical"], b["canonical"], "affiliated_with", sent.text.strip()))
                    connected.add(frozenset((a["canonical"], b["canonical"])))

    uniq = {}
    for e in entities_in_sent:
        uniq[e["canonical"]] = e
    ent_list = list(uniq.values())

    if len(ent_list) > MAX_ENTITIES_FOR_COOCCURRENCE:
        logger.debug(
            "Skipped co-occurrence fallback: %d entities in one sentence (cap=%d): %.80s",
            len(ent_list), MAX_ENTITIES_FOR_COOCCURRENCE, sent.text,
        )
        return relations

    for i in range(len(ent_list)):
        for j in range(i + 1, len(ent_list)):
            a, b = ent_list[i], ent_list[j]
            if frozenset((a["canonical"], b["canonical"])) in connected:
                continue
            relations.append((a["canonical"], b["canonical"], "mentioned_with", sent.text.strip()))

    return relations