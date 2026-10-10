"""Second brain reads without AI: related notes, the note graph and the dashboard. Read-only."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from app.dashboard import build_dashboard
from app.models import Item, Note
from app.related import MIN_SCORE, NoteDoc, build_index, graph_edges, related_notes
from app.security import get_login_session
from app.services import owned_note

CORPUS_LIMIT = 1000
GRAPH_NEIGHBOURS = 3


def load_docs(db, user_id, *, must_include=None, cap=CORPUS_LIMIT):
    """The user's latest notes with the columns that matter, and their item texts, in two queries."""
    rows = db.execute(
        select(Note.id, Note.title, Note.markdown, Note.category_id, Note.updated_at)
        .where(Note.user_id == user_id).order_by(Note.updated_at.desc(), Note.id).limit(cap)
    ).all()
    if must_include is not None and all(row.id != must_include for row in rows):
        rows = [*rows, *db.execute(
            select(Note.id, Note.title, Note.markdown, Note.category_id, Note.updated_at)
            .where(Note.user_id == user_id, Note.id == must_include)
        ).all()]
    texts = {row.id: [] for row in rows}
    for note_id, text in db.execute(
        select(Item.note_id, Item.text).where(Item.user_id == user_id, Item.note_id.in_(list(texts)))
        .order_by(Item.note_id, Item.position, Item.id)
    ):
        texts[note_id].append(text)
    return [
        NoteDoc(row.id, row.title, row.markdown, tuple(texts[row.id]), row.category_id, row.updated_at)
        for row in rows
    ]


def build_router(database, settings):
    router = APIRouter(prefix="/api/v1", tags=["second-brain"])

    @router.get("/notes/{note_id}/related")
    def related(note_id: str, request: Request, limit: int = Query(5, ge=1, le=20), db=Depends(database)):
        session = get_login_session(request, db)
        owned_note(db, note_id, session.user_id)
        docs = load_docs(db, session.user_id, must_include=note_id)
        index = build_index(docs)
        by_id = {doc.id: doc for doc in docs}
        return {"note_id": note_id, "related": [
            {
                "note_id": hit["note_id"], "title": by_id[hit["note_id"]].title,
                "category_id": by_id[hit["note_id"]].category_id,
                "updated_at": by_id[hit["note_id"]].updated_at,
                "score": hit["score"], "shared_terms": hit["shared_terms"],
            }
            for hit in related_notes(index, note_id, limit, MIN_SCORE)
        ]}

    @router.get("/graph")
    def graph(request: Request, limit: int = Query(200, ge=1, le=CORPUS_LIMIT), db=Depends(database)):
        session = get_login_session(request, db)
        docs = load_docs(db, session.user_id)
        index = build_index(docs)
        nodes = docs[:limit]  # Newest first, the same order the query used.
        return {
            "nodes": [
                {"id": doc.id, "title": doc.title, "category_id": doc.category_id, "updated_at": doc.updated_at}
                for doc in nodes
            ],
            "edges": graph_edges(index, [doc.id for doc in nodes], GRAPH_NEIGHBOURS, MIN_SCORE),
        }

    @router.get("/dashboard")
    def dashboard(request: Request, days: int = Query(30, ge=1, le=366), db=Depends(database)):
        session = get_login_session(request, db)
        return build_dashboard(db, settings, session.user_id, days)

    return router
