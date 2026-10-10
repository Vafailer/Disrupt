"""AI assistant requests: creation with limits, worker processing and API views.

Every read is scoped to the owner. The model gets only the notes chosen by
app.assistant_retrieval and answers are accepted only when their quotes are found in those notes.
"""

import hashlib
import json
import time
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.assistant_retrieval import (
    context_for_ask,
    context_for_digest,
    context_for_recommend,
)
from app.error_logging import log_error
from app.limits import ASSISTANT_UNIT_COST, day_window, within_daily_limit
from app.models import AssistantRequest, Item, Note, ProviderUsage, User, new_id
from app.providers import (
    NOT_FOUND_ANSWER,
    CloudRuProvider,
    MockProvider,
    ProviderError,
    validate_assistant,
)
from app.services import lock_account

KINDS = ("ask", "recommend", "digest")
LIMIT_MESSAGE = "На сегодня лимит ИИ закончился. Он обновится завтра."
ERROR_MESSAGES = {
    "ungrounded_quote": "Ответ не удалось подтвердить вашими заметками, поэтому он не показан.",
    "budget_exhausted": "Общий лимит запросов к ИИ закончился. Попробуйте позже.",
    "execution_unknown": "Не удалось узнать, чем закончился запрос. Повторите его позже.",
}
DEFAULT_ERROR_MESSAGE = "ИИ не ответил. Попробуйте ещё раз позже."


class AssistantLimitReached(Exception):
    def __init__(self, resets_at):
        self.resets_at = resets_at
        super().__init__(LIMIT_MESSAGE)


def request_hash(kind, question, days):
    raw = json.dumps([kind, question, days], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def create_request(db, settings, user_id, kind, key, *, question=None, days=None):
    """Queue one assistant action. Costs one unit; a replay with the same key costs nothing."""
    lock_account(db, user_id)
    digest = request_hash(kind, question, days)
    existing = db.scalar(select(AssistantRequest).where(
        AssistantRequest.user_id == user_id, AssistantRequest.idempotency_key == key,
    ))
    if existing:
        if existing.request_hash != digest:
            raise HTTPException(409, "Этот Idempotency-Key уже использован для другого запроса")
        return existing
    user = db.get(User, user_id)
    if kind == "recommend" and not user.assistant_recommendations_enabled:
        raise HTTPException(409, "Рекомендации ИИ выключены. Включите их в настройках помощника")
    if not within_daily_limit(db, settings, user_id, ASSISTANT_UNIT_COST):
        raise AssistantLimitReached(day_window(settings)[1].isoformat())
    pending = db.scalar(select(func.count()).select_from(AssistantRequest).where(
        AssistantRequest.user_id == user_id, AssistantRequest.status.in_(["queued", "running"]),
    ))
    if pending >= settings.max_pending_per_user:
        raise HTTPException(429, "Слишком много запросов помощника ожидают ответа")
    row = AssistantRequest(
        id=new_id(), user_id=user_id, kind=kind, status="queued", question=question, days=days,
        input_note_ids=[], idempotency_key=key, request_hash=digest, units=ASSISTANT_UNIT_COST,
        created_at=time.time(),
    )
    try:
        db.add(row)
        db.commit()
    except IntegrityError:
        db.rollback()
        found = db.scalar(select(AssistantRequest).where(
            AssistantRequest.user_id == user_id, AssistantRequest.idempotency_key == key,
        ))
        if found and found.request_hash == digest:
            return found
        raise HTTPException(409, "Этот Idempotency-Key уже использован для другого запроса") from None
    return row


def owned_request(db, request_id, user_id):
    row = db.scalar(select(AssistantRequest).where(
        AssistantRequest.id == request_id, AssistantRequest.user_id == user_id,
    ))
    if row is None:
        raise HTTPException(404, "Запрос не найден")
    return row


def _titles(db, user_id, note_ids):
    if not note_ids:
        return {}
    rows = db.execute(select(Note.id, Note.title).where(Note.user_id == user_id, Note.id.in_(set(note_ids))))
    return dict(rows.all())


def enrich_result(db, row):
    """Add note titles (and task texts) for display. Stored ids never leave the owner's scope."""
    result = row.result
    if not result:
        return None
    if row.kind == "ask":
        titles = _titles(db, row.user_id, [c["note_id"] for c in result["citations"]])
        return {**result, "citations": [{**c, "note_title": titles.get(c["note_id"])} for c in result["citations"]]}
    if row.kind == "recommend":
        titles = _titles(db, row.user_id, [n for s in result["suggestions"] for n in s["note_ids"]])
        return {**result, "suggestions": [
            {**s, "notes": [{"id": n, "title": titles.get(n)} for n in s["note_ids"]]}
            for s in result["suggestions"]
        ]}
    titles = _titles(db, row.user_id, [h["note_id"] for h in result["highlights"]])
    tasks = {}
    if result["open_tasks"]:
        tasks = {
            item.id: item for item in db.scalars(select(Item).where(
                Item.user_id == row.user_id, Item.id.in_(set(result["open_tasks"])),
            ))
        }
        titles.update(_titles(db, row.user_id, [item.note_id for item in tasks.values()]))
    return {
        **result,
        "highlights": [{**h, "note_title": titles.get(h["note_id"])} for h in result["highlights"]],
        "open_tasks": [
            {"id": task_id, "text": tasks[task_id].text, "note_id": tasks[task_id].note_id,
             "note_title": titles.get(tasks[task_id].note_id)}
            for task_id in result["open_tasks"] if task_id in tasks
        ],
    }


def request_view(db, row, *, detail=True):
    view = {
        "id": row.id, "kind": row.kind, "status": row.status, "question": row.question, "days": row.days,
        "units": row.units, "error_code": row.error_code,
        "error_message": (ERROR_MESSAGES.get(row.error_code, DEFAULT_ERROR_MESSAGE) if row.error_code else None),
        "created_at": row.created_at, "started_at": row.started_at, "finished_at": row.finished_at,
    }
    if detail:
        view["input_note_ids"] = row.input_note_ids
        view["result"] = enrich_result(db, row)
    return view


def empty_result(kind):
    if kind == "ask":
        return {"answer_markdown": NOT_FOUND_ANSWER, "citations": []}
    if kind == "recommend":
        return {"suggestions": []}
    return {"summary_markdown": "За этот период заметок нет.", "highlights": [], "open_tasks": [], "themes": []}


# ---- worker side ----

def claim_request(worker):
    now = time.time()
    with worker.sessions() as db:
        expired = db.execute(
            update(AssistantRequest)
            .where(AssistantRequest.status == "running", AssistantRequest.lease_until < now)
            .values(status="failed", error_code="execution_unknown", finished_at=now)
            .returning(AssistantRequest.id)
        ).all()
        if expired:
            log_error("assistant_lease_expired", "execution_unknown", log_file=worker.settings.error_log_file)
        request_id = db.scalar(
            select(AssistantRequest.id).where(AssistantRequest.status == "queued")
            .order_by(AssistantRequest.created_at, AssistantRequest.id).limit(1)
        )
        if request_id is None:
            db.commit()
            return None
        claimed = db.execute(
            update(AssistantRequest)
            .where(AssistantRequest.id == request_id, AssistantRequest.status == "queued")
            .values(status="running", lease_until=now + worker.settings.lease_seconds)
        ).rowcount
        db.commit()
        return request_id if claimed else None


def fail_request(worker, request_id, code, *, exception=None, http_status=None):
    log_error(
        "assistant_failed", code, log_file=worker.settings.error_log_file, job_id=request_id,
        http_status=http_status, exception=exception,
    )
    with worker.sessions() as db:
        db.execute(
            update(AssistantRequest)
            .where(AssistantRequest.id == request_id, AssistantRequest.status == "running")
            .values(status="failed", error_code=code, finished_at=time.time())
        )
        db.commit()


def finish_request(worker, request_id, result, *, units=None):
    now = time.time()
    values = {"status": "succeeded", "result": result, "finished_at": now, "error_code": None}
    if units is not None:
        values["units"] = units
    with worker.sessions() as db:
        done = db.execute(
            update(AssistantRequest)
            .where(
                AssistantRequest.id == request_id, AssistantRequest.status == "running",
                AssistantRequest.lease_until >= now,
            )
            .values(**values)
        ).rowcount
        db.commit()
    if not done:
        fail_request(worker, request_id, "execution_unknown")


def build_context(db, row):
    if row.kind == "ask":
        return context_for_ask(db, row.user_id, row.question or "", row.days or 90)
    if row.kind == "recommend":
        return context_for_recommend(db, row.user_id)
    return context_for_digest(db, row.user_id, row.days or 7)


def model_name(provider):
    if type(provider) is MockProvider:
        return "mock"
    return provider.model_name if isinstance(provider, CloudRuProvider) else "injected"


def run_assistant_once(worker):
    """Process one queued assistant request. Returns False when nothing was queued.

    The provider is called at most once. An uncertain outcome is stored as failed and never retried.
    """
    request_id = claim_request(worker)
    if request_id is None:
        return False
    try:
        with worker.sessions() as db:
            row = db.get(AssistantRequest, request_id)
            if row.status != "running" or row.lease_until < time.time():
                raise ProviderError("execution_unknown")
            kind, user_id, question = row.kind, row.user_id, row.question
            context = build_context(db, row)
            row.input_note_ids = context.note_ids
            db.commit()
        if context.empty():
            # Nothing to send. No provider call, so no unit is spent.
            finish_request(worker, request_id, empty_result(kind), units=0)
            return True
        call = getattr(worker.provider, "assistant_with_usage", None)
        plain = getattr(worker.provider, "assistant", None)
        if call is None and plain is None:
            raise ProviderError("provider_unavailable")
        with worker.sessions() as db:
            if worker.settings.provider == "cloudru":
                worker.reserve_budget(db, user_id)
            receipt_id = start_receipt(db, request_id, user_id, worker.provider)
        started = time.monotonic()
        try:
            def on_usage(values):
                worker.usage.tokens(receipt_id, values)

            raw = (
                call(kind, context, question=question, on_usage=on_usage)
                if call else plain(kind, context, question=question)
            )
            result = validate_assistant(kind, raw, context)
        except Exception as exc:
            ambiguous = isinstance(exc, ProviderError) and (
                exc.code.endswith("_unknown") or exc.code in {"execution_unknown", "provider_unavailable"}
            )
            worker.usage.finish(receipt_id, started, "unknown" if ambiguous else "failed")
            raise
        worker.usage.finish(receipt_id, started, "succeeded")
        finish_request(worker, request_id, result)
    except ProviderError as exc:
        fail_request(worker, request_id, exc.code, exception=exc, http_status=exc.http_status)
    except ValidationError as exc:
        fail_request(worker, request_id, "provider_invalid_response", exception=exc)
    except Exception as exc:
        fail_request(worker, request_id, "internal_error", exception=exc)
    return True


def start_receipt(db, request_id, user_id, provider):
    """Receipt and `started_at` are saved before the call. From here the unit counts as spent."""
    lock_account(db, user_id)
    now = time.time()
    mock = type(provider) is MockProvider
    row = ProviderUsage(
        id=new_id(), user_id=user_id, operation_id=request_id, request_id=request_id + ":assistant",
        channel="web", kind="llm", model=model_name(provider), is_test=db.get(User, user_id).is_test,
        tariff_version="mock-no-billing-v1" if mock else None, status="unknown",
        cost=Decimal(0) if mock else None, input_tokens=0 if mock else None, output_tokens=0 if mock else None,
        cache_tokens=0 if mock else None, occurred_at=now,
    )
    db.add(row)
    started = db.execute(
        update(AssistantRequest)
        .where(AssistantRequest.id == request_id, AssistantRequest.status == "running",
               AssistantRequest.lease_until >= now)
        .values(started_at=now)
    ).rowcount
    if not started:
        db.rollback()
        raise ProviderError("execution_unknown")
    db.commit()
    return row.id
