"""Choose notes for the AI assistant without any AI. Everything is scoped to one owner.

The model only ever sees what these functions return. It has no access to the database.
"""

import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import or_, select

from app.models import Item, Note

PROMPT_CHARS = 12000  # Total size of the notes block in one prompt.
ASK_NOTES = 8
ASK_NOTE_CHARS = 2500
RECOMMEND_NOTE_CHARS = 700
DIGEST_NOTE_CHARS = 900
ITEM_CHARS = 200
ITEMS_PER_NOTE = 10
INDEX_LIMIT = 500  # Latest notes considered for "ask".
RECOMMEND_DAYS = 30
OPEN_ITEM_LIMIT = 30
RECENT_NOTE_LIMIT = 60
WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)


@dataclass
class NoteDoc:
    id: str
    title: str
    text: str
    created_at: float
    items: list = field(default_factory=list)  # [{"id", "kind", "text", "status"}]


@dataclass
class AssistantContext:
    """Notes chosen for one request. `corpus` is what quotes are checked against."""

    notes: list
    items: list

    @property
    def note_ids(self):
        return [note["id"] for note in self.notes]

    @property
    def item_ids(self):
        return {item["id"] for item in self.items}

    @property
    def open_task_ids(self):
        return {item["id"] for item in self.items if item["kind"] == "task" and item["status"] == "open"}

    @property
    def corpus(self):
        """note id -> every text a quote may come from (title, shown text, shown items)."""
        return {
            note["id"]: [note["title"], note["text"], *(item["text"] for item in note["items"])]
            for note in self.notes
        }

    def empty(self):
        return not self.notes


def tokenize(text):
    """Lowercase words of 3+ letters. A five letter prefix hides most Russian endings."""
    return [word[:5] for word in WORD.findall((text or "").casefold())]


def rank_notes(question, docs, limit=ASK_NOTES):
    """TF-IDF over title, text and items. Notes without any shared word are dropped."""
    query = set(tokenize(question))
    if not query or not docs:
        return []
    counts = []
    for doc in docs:
        words = tokenize(doc.title) * 2 + tokenize(doc.text) + [w for i in doc.items for w in tokenize(i["text"])]
        counts.append(Counter(words))
    total = len(docs)
    frequency = Counter(word for count in counts for word in count if word in query)
    scored = []
    for doc, count in zip(docs, counts, strict=True):
        size = sum(count.values()) or 1
        score = 0.0
        for word in query:
            if count[word]:
                idf = math.log((total + 1) / (frequency[word] + 1)) + 1
                score += (1 + math.log(count[word])) * idf
        if score > 0:
            scored.append((score / math.sqrt(size), doc.created_at, doc.id, doc))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return [row[3] for row in scored[:limit]]


def _clip(text, size):
    return text if len(text) <= size else text[:size].rstrip()


def _load(db, user_id, notes):
    ids = [note.id for note in notes]
    by_note = {note_id: [] for note_id in ids}
    for start in range(0, len(ids), 400):
        rows = db.scalars(
            select(Item).where(Item.user_id == user_id, Item.note_id.in_(ids[start:start + 400]))
            .order_by(Item.note_id, Item.position, Item.id)
        )
        for item in rows:
            by_note[item.note_id].append(
                {"id": item.id, "kind": item.kind, "text": item.text, "status": item.status}
            )
    return [
        NoteDoc(note.id, note.title, note.markdown, note.created_at, by_note[note.id]) for note in notes
    ]


def _build(docs, note_chars, budget=PROMPT_CHARS):
    """Cut every note, then stop adding notes when the shared size cap is reached."""
    notes, items, used = [], [], 0
    for doc in docs:
        shown = [
            {**item, "text": _clip(item["text"], ITEM_CHARS)} for item in doc.items[:ITEMS_PER_NOTE]
        ]
        entry = {
            "id": doc.id,
            "title": _clip(doc.title, 200),
            "date": datetime.fromtimestamp(doc.created_at, UTC).date().isoformat(),
            "text": _clip(doc.text, note_chars),
            "items": shown,
        }
        size = len(entry["title"]) + len(entry["text"]) + sum(len(i["text"]) + 40 for i in shown) + 80
        if notes and used + size > budget:
            break
        notes.append(entry)
        items.extend(shown)
        used += size
    return AssistantContext(notes, items)


def context_for_ask(db, user_id, question, days=90, now=None):
    now = time.time() if now is None else now
    notes = db.scalars(
        select(Note)
        .where(Note.user_id == user_id, Note.created_at >= now - days * 86400)
        .order_by(Note.created_at.desc(), Note.id)
        .limit(INDEX_LIMIT)
    ).all()
    docs = rank_notes(question, _load(db, user_id, notes))
    return _build(docs, ASK_NOTE_CHARS)


def context_for_recommend(db, user_id, now=None):
    """Notes of the last 30 days plus notes that hold the user's open tasks and ideas."""
    now = time.time() if now is None else now
    recent = db.scalars(
        select(Note)
        .where(Note.user_id == user_id, Note.created_at >= now - RECOMMEND_DAYS * 86400)
        .order_by(Note.created_at.desc(), Note.id)
        .limit(RECENT_NOTE_LIMIT)
    ).all()
    open_note_ids = db.scalars(
        select(Item.note_id)
        .where(Item.user_id == user_id, Item.status == "open", Item.kind.in_(["task", "idea"]))
        .order_by(Item.id)
        .limit(OPEN_ITEM_LIMIT)
    ).all()
    known = {note.id for note in recent}
    extra_ids = [note_id for note_id in dict.fromkeys(open_note_ids) if note_id not in known]
    extra = db.scalars(
        select(Note).where(Note.user_id == user_id, Note.id.in_(extra_ids)).order_by(Note.created_at.desc())
    ).all() if extra_ids else []
    return _build(_load(db, user_id, [*recent, *extra]), RECOMMEND_NOTE_CHARS)


def context_for_digest(db, user_id, days=7, now=None):
    now = time.time() if now is None else now
    since = now - days * 86400
    notes = db.scalars(
        select(Note)
        .where(Note.user_id == user_id, or_(Note.created_at >= since, Note.updated_at >= since))
        .order_by(Note.created_at.desc(), Note.id)
        .limit(RECENT_NOTE_LIMIT)
    ).all()
    return _build(_load(db, user_id, notes), DIGEST_NOTE_CHARS)
