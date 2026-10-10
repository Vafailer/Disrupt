"""Owned structure and explicit user actions. GET requests do not create events."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.analytics import record_event
from app.models import Category, Outbox, ProductEvent, Reminder
from app.schemas import (
    CategoryCreate,
    CategoryEdit,
    CategoryResponse,
    ItemCreate,
    ItemEdit,
    NoteCategoryEdit,
    NoteOpened,
    NoteResponse,
    UserAction,
    VersionRequest,
)
from app.security import get_login_session
from app.services import (
    advance_note,
    category_view,
    change_note_category,
    confirm_structure,
    create_item,
    edit_item,
    ensure_category,
    note_view,
    owned_category,
    owned_note,
    rename_category,
    save_revision,
)


def build_router(database):
    router = APIRouter(prefix="/api/v1", tags=["structure"])

    @router.get("/categories", response_model=list[CategoryResponse])
    def categories(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        rows = db.scalars(
            select(Category).where(Category.user_id == session.user_id).order_by(Category.name, Category.id)
        )
        return [category_view(category) for category in rows]

    @router.post("/categories", response_model=CategoryResponse, status_code=201)
    def add_category(body: CategoryCreate, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        category = ensure_category(db, session.user_id, body.name)
        db.commit()
        return category_view(category)

    @router.patch("/categories/{category_id}", response_model=CategoryResponse)
    def update_category(category_id: str, body: CategoryEdit, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        category = owned_category(db, category_id, session.user_id)
        return category_view(rename_category(db, category, body.name, body.version))

    @router.patch("/notes/{note_id}/category", response_model=NoteResponse)
    def update_note_category(note_id: str, body: NoteCategoryEdit, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        change_note_category(db, note, body.category_id, body.version)
        return note_view(db, note, request.app.state.settings)

    @router.post("/notes/{note_id}/items", status_code=201, response_model=NoteResponse)
    def add_item(note_id: str, body: ItemCreate, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        create_item(db, note, body)
        return note_view(db, note, request.app.state.settings)

    @router.patch("/notes/{note_id}/items/{item_id}", response_model=NoteResponse)
    def update_item(note_id: str, item_id: str, body: ItemEdit, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        edit_item(db, note, item_id, body)
        return note_view(db, note, request.app.state.settings)

    @router.post("/notes/{note_id}/confirm-structure", response_model=NoteResponse)
    def confirm(note_id: str, body: VersionRequest, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        note = owned_note(db, note_id, session.user_id)
        advance_note(db, note, body.version)
        confirm_structure(db, note)
        db.flush()
        save_revision(db, note)
        db.commit()
        return note_view(db, note, request.app.state.settings)

    @router.post("/search/events", status_code=204)
    def search_event(body: UserAction, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        record_event(db, session.user_id, "search_submitted", body.operation_id)
        db.commit()

    @router.post("/notes/{note_id}/opened", status_code=204)
    def opened(note_id: str, body: NoteOpened, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        owned_note(db, note_id, session.user_id)
        if body.reminder_id is not None:
            reminder = db.scalar(select(Reminder).where(
                Reminder.id == body.reminder_id, Reminder.user_id == session.user_id, Reminder.note_id == note_id,
            ))
            delivered = db.scalar(select(Outbox.id).where(
                Outbox.reminder_id == body.reminder_id, Outbox.user_id == session.user_id,
                Outbox.status.in_({"sent", "unknown"}), Outbox.authorized_at.is_not(None),
            ))
            if reminder is None or delivered is None:
                raise HTTPException(404, "Напоминание не найдено")
        if body.search_operation_id is not None:
            search = db.scalar(
                select(ProductEvent).where(
                    ProductEvent.user_id == session.user_id,
                    ProductEvent.name == "search_submitted",
                    ProductEvent.operation_id == body.search_operation_id,
                )
            )
            if search is None:
                raise HTTPException(422, "Поиск не найден. Повторите поиск")
        record_event(db, session.user_id, "note_opened", body.operation_id, subject_id=note_id)
        if body.search_operation_id is not None:
            record_event(db, session.user_id, "search_result_opened", body.operation_id, subject_id=note_id)
        if body.reminder_id is not None:
            record_event(db, session.user_id, "reminder_opened", body.operation_id, subject_id=note_id)
        db.commit()

    @router.post("/notes/{note_id}/original-opened", status_code=204)
    def original_opened(note_id: str, body: UserAction, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        owned_note(db, note_id, session.user_id)
        record_event(db, session.user_id, "original_opened", body.operation_id)
        db.commit()

    return router
