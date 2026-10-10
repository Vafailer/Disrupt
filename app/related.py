"""Related notes without AI. TF-IDF cosine over one user's notes, computed in Python on request.

Fields are weighted: title 3, task and idea texts 2, markdown 1. Words are lowercased, ё becomes е,
stop words are dropped and Russian endings are trimmed to a short stem, so "проекта" and "проекты"
meet. Nothing here talks to a model or to the database; the caller passes plain NoteDoc rows.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

TITLE_WEIGHT, ITEM_WEIGHT, MARKDOWN_WEIGHT = 3, 2, 1
CATEGORY_BONUS = 0.05
MIN_SCORE = 0.12
MAX_SHARED_TERMS = 5
MAX_MARKDOWN_CHARS = 20000
# A word that sits in more notes than this adds no meaning and would make the graph quadratic.
POSTING_LIMIT = 300
STEM_LIMIT = 6
# Only the strongest words of a note take part, which keeps 1000 notes fast and drops noise.
MAX_TERMS = 60

WORD = re.compile(r"[a-zа-яё0-9]+")
STOP_WORDS = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня
еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там
потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже
себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
сейчас были куда зачем всех никогда можно при два другой хоть после над больше тот через эти нас про всего них
какая много разве три эту моя впрочем свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно
всю между это эта этих такие которые который которая которое которых которым нужно надо очень просто также
the and for are but not you all any can had her was one our out has have this that with from they will would
there their what about which when your
""".split())
ENDINGS = tuple(sorted("""
ировании ировать ирование ованием ование ления лениях лением ениями ением ениям
ами ями ого его ому ему ыми ими ией ией ому ешь ишь ают яют ует уют
ий ый ой ая яя ое ее ые ие ых их ом ем ам ям ах ях ую юю ов ев ей ия ть ти ет ут ют ит ат ят
ить ать еть ять уть а я ы и е у ю о ь й
""".split(), key=len, reverse=True))
ENGLISH_ENDINGS = ("ing", "ed", "s")


def stem(word):
    """Crude stem of a lowercase word with ё already replaced."""
    if word.isascii():
        for ending in ENGLISH_ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                word = word[:-len(ending)]
                break
        return word[:STEM_LIMIT]
    for ending in ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            word = word[:-len(ending)]
            break
    return word[:STEM_LIMIT]


def terms(text):
    """(stem, original lowercase word) for every meaningful word of the text."""
    found = []
    for token in WORD.findall((text or "").lower()):
        plain = token.replace("ё", "е")
        if len(plain) < 3 or plain.isdigit() or plain in STOP_WORDS:
            continue
        found.append((stem(plain), token))
    return found


@dataclass(frozen=True)
class NoteDoc:
    id: str
    title: str
    markdown: str
    items: tuple
    category_id: str | None
    updated_at: float


class Index:
    def __init__(self, docs):
        self.docs = {doc.id: doc for doc in docs}
        counts, self.surface = {}, {}
        for doc in self.docs.values():
            counter, surface = Counter(), {}
            for text, weight in (
                (doc.title, TITLE_WEIGHT), (" ".join(doc.items), ITEM_WEIGHT),
                ((doc.markdown or "")[:MAX_MARKDOWN_CHARS], MARKDOWN_WEIGHT),
            ):
                for root, word in terms(text):
                    counter[root] += weight
                    surface.setdefault(root, word)
            counts[doc.id], self.surface[doc.id] = counter, surface
        frequency = Counter(root for counter in counts.values() for root in counter)
        total = len(self.docs)
        self.vectors, self.postings = {}, {}
        for note_id, counter in counts.items():
            raw = {
                root: (1 + math.log(weight)) * (math.log((1 + total) / (1 + frequency[root])) + 1)
                for root, weight in counter.items()
            }
            top = dict(sorted(raw.items(), key=lambda pair: (-pair[1], pair[0]))[:MAX_TERMS])
            norm = math.sqrt(sum(value * value for value in top.values())) or 1.0
            vector = {root: value / norm for root, value in top.items()}
            self.vectors[note_id] = vector
            for root, value in vector.items():
                if frequency[root] <= POSTING_LIMIT:
                    self.postings.setdefault(root, []).append((note_id, value))

    def scores(self, note_id, allowed=None):
        """Cosine plus category bonus against every other note that shares at least one stem."""
        base = self.docs[note_id]
        found = {}
        for root, mine in self.vectors[note_id].items():
            for other_id, theirs in self.postings.get(root, ()):
                if other_id != note_id and (allowed is None or other_id in allowed):
                    found[other_id] = found.get(other_id, 0.0) + mine * theirs
        if base.category_id is not None:
            for other_id in found:
                if self.docs[other_id].category_id == base.category_id:
                    found[other_id] += CATEGORY_BONUS
        return found

    def shared_terms(self, note_id, other_id):
        mine, theirs = self.vectors[note_id], self.vectors[other_id]
        roots = sorted((r for r in mine if r in theirs), key=lambda r: (-mine[r] * theirs[r], r))
        words = []
        for root in roots:
            word = self.surface[note_id][root]
            if word not in words:
                words.append(word)
        return words[:MAX_SHARED_TERMS]

    def order(self, found):
        docs = self.docs
        return sorted(found, key=lambda other: (-found[other], -docs[other].updated_at, other))


def build_index(docs):
    return Index(docs)


def related_notes(index, note_id, limit=5, min_score=MIN_SCORE):
    """Other notes ranked by similarity: [{note_id, score, shared_terms}]. Empty for an unknown id."""
    if note_id not in index.docs:
        return []
    found = index.scores(note_id)
    result = []
    for other_id in index.order(found):
        score = min(1.0, found[other_id])
        if score < min_score:
            break
        result.append({
            "note_id": other_id, "score": round(score, 4),
            "shared_terms": index.shared_terms(note_id, other_id),
        })
        if len(result) >= limit:
            break
    return result


def rank_related(docs, note_id, limit=5, min_score=MIN_SCORE):
    """Pure helper: rank the other documents of `docs` against `note_id`."""
    return related_notes(build_index(docs), note_id, limit, min_score)


def graph_edges(index, node_ids, per_note=3, min_score=MIN_SCORE):
    """Top neighbours of every node inside `node_ids`, one edge per pair, strongest first."""
    allowed = set(node_ids)
    best = {}
    for note_id in node_ids:
        if note_id not in index.docs:
            continue
        found = index.scores(note_id, allowed)
        kept = 0
        for other_id in index.order(found):
            score = min(1.0, found[other_id])
            if score < min_score or kept >= per_note:
                break
            kept += 1
            pair = tuple(sorted((note_id, other_id)))
            best[pair] = max(best.get(pair, 0.0), score)
    return [
        {"source": a, "target": b, "score": round(score, 4)}
        for (a, b), score in sorted(best.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
