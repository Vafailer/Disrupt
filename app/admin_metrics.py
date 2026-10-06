"""Moscow calendar aggregates over content-free events and provider receipts."""

import csv
import hashlib
import hmac
import io
import math
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from datetime import time as day_time
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from app.analytics import BACKGROUND_EVENTS
from app.contracts import AdminSummary
from app.models import Capture, Job, Note, ProductEvent, ProviderUsage, TranscriptRevision, User

MOSCOW = ZoneInfo("Europe/Moscow")
ACTIVE = {"capture_saved", "note_opened", "note_edited", "reminder_confirmed", "task_completed"}
RETURN = {"search_result_opened", "reminder_opened"}
DAY = 86400


def calendar_day(timestamp):
    return datetime.fromtimestamp(timestamp, MOSCOW).date()


def midnight(value):
    return datetime.combine(value, day_time(), MOSCOW).timestamp()


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def percentage(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": 100.0 * numerator / denominator if denominator else None}


def money(value):
    return None if value is None else format(value.quantize(Decimal("0.00000001")), "f")


def divide(value, count):
    return money(value / count) if value is not None and count else None


def period(start, end, now):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(422, "Проверьте даты периода") from None
    if first > last or (last - first).days >= 366 or last > calendar_day(now):
        raise HTTPException(422, "Выберите период до сегодняшнего дня, не длиннее 366 дней")
    return first, last


def pseudonym(key, domain, identifier):
    if identifier is None:
        return None
    return domain + "_" + hmac.new(key, (domain + ":" + identifier).encode(), hashlib.sha256).hexdigest()[:32]


def known_sum(rows, field):
    values = [getattr(row, field) for row in rows]
    return None if any(value is None for value in values) else sum((Decimal(str(value)) for value in values), Decimal(0))


def unknown(row):
    fields = ("cost", "input_tokens", "output_tokens", "cache_tokens") if row.kind == "llm" else ("cost", "stt_seconds")
    return any(getattr(row, field) is None for field in fields)


def costs(rows, users, sessions, dau_sum):
    llm, stt = ([row for row in rows if row.kind == kind] for kind in ("llm", "stt"))
    llm_cost, stt_cost = known_sum(llm, "cost"), known_sum(stt, "cost")
    total = llm_cost + stt_cost if llm_cost is not None and stt_cost is not None else None
    return {
        "currency": "RUB", "llm_cost": money(llm_cost), "stt_cost": money(stt_cost),
        "known_llm_cost": money(sum((r.cost for r in llm if r.cost is not None), Decimal(0))),
        "known_stt_cost": money(sum((r.cost for r in stt if r.cost is not None), Decimal(0))),
        "unknown_usage_calls": sum(unknown(row) for row in rows), "llm_calls": len(llm), "stt_calls": len(stt),
        **{name: None if (value := known_sum(llm, name)) is None else int(value)
           for name in ("input_tokens", "output_tokens", "cache_tokens")},
        "stt_minutes": divide(known_sum(stt, "stt_seconds"), 60),
        "user_count": users, "session_count": sessions, "dau_sum": dau_sum,
        "cost_per_user": divide(total, users), "cost_per_session": divide(total, sessions),
        "llm_cost_per_dau": divide(llm_cost, dau_sum),
    }


def subject(event, note_ids):
    if event.subject_id in note_ids:
        return event.subject_id
    # Older checks carry a server-authored note UUID. Opaque opened operations cannot be recovered.
    head = event.operation_id.split(":", 1)[0]
    return head if ":" in event.operation_id and head in note_ids else None


def completion(events, captures, notes, mode, first, last):
    saved = {event.operation_id: event.occurred_at for event in events
             if event.name == "capture_saved" and first <= event.occurred_at <= last}
    opened, finished = {}, {}
    for event in events:
        if not first <= event.occurred_at <= last:
            continue
        note_id = subject(event, notes)
        if note_id is None:
            continue
        if event.name == "note_opened":
            opened.setdefault(note_id, event.occurred_at)
        final = {"structure_confirmed", "note_edited"} if mode == "ai" else {"search_result_opened", "reminder_confirmed"}
        if event.name in final and note_id in opened and opened[note_id] <= event.occurred_at:
            capture_id = notes[note_id]
            if (capture_id in saved and captures.get(capture_id) == mode
                    and saved[capture_id] <= opened[note_id]):
                finished.setdefault(note_id, event.occurred_at)
    return min(finished.values(), default=None)


def legacy_usage(db, users, receipts, low, high, channel):
    """Missing pre-ledger successful stages stay unknown, rather than a fabricated zero bill."""
    existing = {(row.operation_id, row.kind) for row in receipts}
    tracked_jobs = {row.operation_id for row in receipts}
    jobs = db.execute(select(Job, Capture.channel, Capture.input_kind, Capture.audio_seconds).join(Capture, Capture.id == Job.capture_id).where(
        Job.user_id.in_(users), Job.finished_at >= low, Job.finished_at < high,
        Job.status.in_({"succeeded", "failed"}),
    )).all()
    test_operations = {row.operation_id for row in db.execute(select(ProductEvent.operation_id).where(
        ProductEvent.user_id.in_(users), ProductEvent.is_test.is_(True),
        ProductEvent.name.in_({"capture_saved", "processing_completed", "processing_failed"}),
    ))}
    rows = []
    for job, job_channel, input_kind, seconds in jobs:
        if job.id in test_operations or job.capture_id in test_operations:
            continue
        if channel != "all" and job_channel != channel:
            continue
        if (job.id, "llm") in existing:
            continue
        uncertain = job.error_code in {"execution_unknown", "internal_error"} and job.id not in tracked_jobs
        proven = job.status == "succeeded" or (job.error_code or "").startswith("provider_") or job.error_code == "ungrounded_quote"
        if not proven and not uncertain:
            continue
        rows.append(SimpleNamespace(
            id="legacy:" + job.id, user_id=job.user_id, operation_id=job.id, session_id=None,
            channel=job_channel, kind="llm", model="legacy-untracked", tariff_version=None, status="unknown",
            occurred_at=job.finished_at, input_tokens=None, output_tokens=None, cache_tokens=None,
            stt_seconds=None, cost=None, estimated_cost=None, latency_ms=None,
        ))
        if uncertain and input_kind == "audio":
            rows.append(SimpleNamespace(
                id="legacy-stt:" + job.capture_id, user_id=job.user_id, operation_id=job.id, session_id=None,
                channel=job_channel, kind="stt", model="legacy-untracked", tariff_version=None, status="unknown",
                occurred_at=job.finished_at, input_tokens=None, output_tokens=None, cache_tokens=None,
                stt_seconds=seconds, cost=None, estimated_cost=None, latency_ms=None,
            ))
    # A persisted first STT revision proves a completed STT stage, even before usage was added.
    revisions = db.execute(select(TranscriptRevision.capture_id, TranscriptRevision.created_at,
                                  Job.id, Job.user_id, Capture.channel, Capture.audio_seconds)
                          .join(Capture, Capture.id == TranscriptRevision.capture_id)
                          .join(Job, Job.capture_id == Capture.id).where(
                              Job.user_id.in_(users), TranscriptRevision.origin == "stt",
                              TranscriptRevision.created_at >= low, TranscriptRevision.created_at < high,
                          )).all()
    for capture_id, occurred, job_id, user_id, job_channel, seconds in revisions:
        if capture_id in test_operations or job_id in test_operations:
            continue
        if (job_id, "stt") in existing or channel != "all" and channel != job_channel:
            continue
        if any(row.operation_id == job_id and row.kind == "stt" for row in rows):
            continue
        rows.append(SimpleNamespace(
            id="legacy-stt:" + capture_id, user_id=user_id, operation_id=job_id, session_id=None,
            channel=job_channel, kind="stt", model="legacy-untracked", tariff_version=None, status="unknown",
            occurred_at=occurred, input_tokens=None, output_tokens=None, cache_tokens=None,
            stt_seconds=seconds, cost=None, estimated_cost=None, latency_ms=None,
        ))
    return rows


def aggregate(db, start, end, channel, source, key, *, usage_limit=50, usage_offset=0, now=None):
    now = time.time() if now is None else now
    first, last = period(start, end, now)
    observed_end = math.nextafter(now, math.inf)
    low, high = midnight(first), min(midnight(last + timedelta(days=1)), observed_end)
    horizon = min(midnight(last + timedelta(days=9)), observed_end)
    candidates = or_(
        and_(User.first_login_at >= low, User.first_login_at < high),
        select(ProductEvent.id).where(ProductEvent.user_id == User.id, ProductEvent.occurred_at >= low,
                                      ProductEvent.occurred_at < high).exists(),
        select(ProviderUsage.id).where(ProviderUsage.user_id == User.id, ProviderUsage.occurred_at >= low,
                                       ProviderUsage.occurred_at < high).exists(),
        select(Job.id).where(Job.user_id == User.id, Job.finished_at >= low, Job.finished_at < high).exists(),
        select(TranscriptRevision.id).join(Capture, Capture.id == TranscriptRevision.capture_id).where(
            Capture.user_id == User.id, TranscriptRevision.created_at >= low, TranscriptRevision.created_at < high,
        ).exists(),
    )
    user_query = select(User.id, User.first_login_at, User.created_at).where(User.is_test.is_(False), candidates)
    if source != "all":
        user_query = user_query.where(User.source == source)
    users = {row.id: row for row in db.execute(user_query)}
    event_query = select(ProductEvent).join(User, User.id == ProductEvent.user_id).where(
        ProductEvent.user_id.in_(users), ProductEvent.is_test.is_(False), ProductEvent.occurred_at <= now,
        or_(and_(ProductEvent.occurred_at >= low, ProductEvent.occurred_at < horizon),
            and_(ProductEvent.occurred_at >= User.first_login_at,
                 ProductEvent.occurred_at <= User.first_login_at + DAY)),
    ).order_by(ProductEvent.occurred_at, ProductEvent.id)
    if channel != "all":
        event_query = event_query.where(ProductEvent.channel == channel)
    events = list(db.scalars(event_query))
    order = {"capture_saved": 0, "note_opened": 1, "structure_confirmed": 2, "note_edited": 2}
    events.sort(key=lambda e: (e.occurred_at, order.get(e.name, 3), e.id))
    selected = [e for e in events if low <= e.occurred_at < high]
    by_user = defaultdict(list)
    selected_by_user = defaultdict(list)
    selected_by_day = defaultdict(list)
    for event in selected:
        selected_by_user[event.user_id].append(event)
        selected_by_day[calendar_day(event.occurred_at)].append(event)
    for event in events:
        by_user[event.user_id].append(event)
    capture_ids = {e.operation_id for e in events if e.name == "capture_saved"}
    captures = dict(db.execute(select(Capture.id, Capture.processing_mode).where(Capture.id.in_(capture_ids))).all())
    notes = {row.id: row.capture_id for row in db.execute(
        select(Note.id, Note.capture_id).where(Note.capture_id.in_(capture_ids))
    )}
    foreground = [e for e in selected if e.name not in BACKGROUND_EVENTS]
    active_users = {e.user_id for e in selected if e.name in ACTIVE}
    sessions = {e.session_id for e in foreground}
    seen = {e.user_id for e in foreground}
    cohort = {uid for uid, user in users.items() if user.first_login_at is not None
              and low <= user.first_login_at < high and uid in seen}
    mature = {uid for uid in cohort if users[uid].first_login_at + DAY <= now}
    ai, manual = {}, {}
    for uid, user in users.items():
        if user.first_login_at is None:
            continue
        own_events = by_user[uid]
        for mode, target in (("ai", ai), ("manual", manual)):
            done = completion(own_events, captures, notes, mode, user.first_login_at, min(user.first_login_at + DAY, now))
            if done is not None:
                target[uid] = done
    activated = {uid: min(t for t in (ai.get(uid), manual.get(uid)) if t is not None)
                 for uid in ai.keys() | manual.keys()}
    retention_cohort = {uid for uid in cohort if uid in activated
                        and calendar_day(activated[uid]) == calendar_day(users[uid].first_login_at)}
    retention = {}
    today = calendar_day(now)
    for offset in (1, 7):
        eligible = {uid for uid in retention_cohort
                    if users[uid].first_login_at + DAY <= now
                    and calendar_day(users[uid].first_login_at) + timedelta(days=offset) < today}
        retained = {uid for uid in eligible if any(
            e.name in ACTIVE and calendar_day(e.occurred_at) == calendar_day(users[uid].first_login_at) + timedelta(days=offset)
            for e in by_user[uid]
        )}
        retention[f"d{offset}"] = percentage(len(retained), len(eligible))
        retention[f"pending_d{offset}"] = len(retention_cohort - eligible)
    returns = {e.user_id for e in selected if e.name in RETURN and e.user_id in activated
               and e.occurred_at > activated[e.user_id]}
    completed = {uid for uid in active_users if any(
        completion(by_user[uid], captures, notes, mode, low, math.nextafter(high, -math.inf)) is not None
        for mode in ("ai", "manual")
    )}
    stages = [cohort, set(), set(), set(), set()]
    for uid in cohort:
        own = selected_by_user[uid]
        saved = {e.operation_id: e.occurred_at for e in own if e.name == "capture_saved"}
        opened = {subject(e, notes): e.occurred_at for e in reversed(own) if e.name == "note_opened"}
        matching = {nid: t for nid, t in opened.items() if notes.get(nid) in saved and saved[notes[nid]] <= t}
        checked = [e.occurred_at for e in own if e.name in {"structure_confirmed", "note_edited"}
                   and subject(e, notes) in matching and matching[subject(e, notes)] <= e.occurred_at]
        if saved:
            stages[1].add(uid)
        if matching:
            stages[2].add(uid)
        if checked:
            stages[3].add(uid)
            if any(e.name in RETURN and e.occurred_at > min(checked) for e in own) and uid in returns:
                stages[4].add(uid)
    receipts_query = select(ProviderUsage).where(
        ProviderUsage.user_id.in_(users), ProviderUsage.is_test.is_(False),
        ProviderUsage.occurred_at >= low, ProviderUsage.occurred_at < high,
    )
    if channel != "all":
        receipts_query = receipts_query.where(ProviderUsage.channel == channel)
    receipts = list(db.scalars(receipts_query))
    # Look up all stage receipts for candidate legacy jobs: a call spanning midnight must not be counted twice.
    recorded = list(db.execute(select(ProviderUsage.operation_id, ProviderUsage.kind).where(ProviderUsage.user_id.in_(users))))
    usage = receipts + legacy_usage(db, users, recorded, low, high, channel)
    usage.sort(key=lambda row: (row.occurred_at, row.id), reverse=True)
    usage_by_day = defaultdict(list)
    for row in usage:
        usage_by_day[calendar_day(row.occurred_at)].append(row)
    daily = []
    cursor = first
    while cursor <= last:
        day_events = selected_by_day[cursor]
        daily_users = {e.user_id for e in day_events if e.name in ACTIVE}
        new = {uid for uid in daily_users if users[uid].first_login_at is not None
               and calendar_day(users[uid].first_login_at) == cursor}
        returning = {uid for uid in daily_users if users[uid].first_login_at is not None
                     and calendar_day(users[uid].first_login_at) < cursor}
        rows = usage_by_day[cursor]
        day_costs = costs(rows, len(daily_users), len({e.session_id for e in day_events if e.name not in BACKGROUND_EVENTS}), len(daily_users))
        daily.append({"date": cursor.isoformat(), "registrations": len({e.user_id for e in day_events if e.name == "registered"}),
                      "dau": len(daily_users), "new_users": len(new), "returning_users": len(returning),
                      **{name: day_costs[name] for name in ("llm_cost", "stt_cost", "unknown_usage_calls", "session_count", "llm_cost_per_dau")}})
        cursor += timedelta(days=1)
    last_day = last if last < today else today - timedelta(days=1) if first < today else today
    dau = next((row["dau"] for row in daily if row["date"] == last_day.isoformat()), 0)
    quality_success = {e.operation_id for e in selected if e.name == "processing_completed"}
    quality_fail = {e.operation_id for e in selected if e.name == "processing_failed"} - quality_success
    latencies = sorted(max(0, int((row.finished_at - row.created_at) * 1000)) for row in db.execute(
        select(Job.finished_at, Job.created_at).where(Job.id.in_(quality_success | quality_fail), Job.finished_at.is_not(None))
    ))
    deliveries = {}
    for event in sorted(selected, key=lambda e: (e.occurred_at, e.name == "reminder_result", e.id)):
        if event.name in {"reminder_sent", "reminder_failed", "reminder_result"}:
            deliveries[event.operation_id] = event.outcome or ("sent" if event.name == "reminder_sent" else "failed")
    summary = {
        "cards": {"registrations": sum(row["registrations"] for row in daily), "dau": dau,
                  "unique_users": len(active_users), "new_users": len(active_users & cohort),
                  "returning_users": len({uid for uid in active_users if users[uid].first_login_at is not None and users[uid].first_login_at < low}),
                  "ai_activation": percentage(len(mature & ai.keys()), len(mature)),
                  "manual_activation": percentage(len(mature & manual.keys()), len(mature)),
                  "activation_pending": len(cohort - mature), "completed_scenario": len(completed), "returns": len(returns),
                  "d1": retention["d1"], "d7": retention["d7"]},
        "daily": daily, "retention": retention,
        "funnel": [{"step": step, "users": len(group), "conversion": percentage(len(group), len(cohort))}
                   for step, group in zip(("registered", "capture_saved", "note_opened", "structure_checked", "returned"), stages, strict=True)],
        "usage": [{"id": pseudonym(key, "receipt", row.id), "user_pseudonym": pseudonym(key, "user", row.user_id),
                   "session_pseudonym": pseudonym(key, "session", row.session_id), "operation_pseudonym": pseudonym(key, "operation", row.operation_id),
                   "occurred_at": utc(row.occurred_at), **{name: getattr(row, name) for name in (
                       "channel", "kind", "model", "tariff_version", "status", "input_tokens", "output_tokens", "cache_tokens", "latency_ms")},
                   "stt_seconds": float(row.stt_seconds) if row.stt_seconds is not None else None,
                   "cost": money(row.cost), "estimated_cost": money(row.estimated_cost)}
                  for row in usage[usage_offset:usage_offset + usage_limit]],
        "quality": {"ai_succeeded": len(quality_success), "ai_failed": len(quality_fail),
                    "ai_success_rate": percentage(len(quality_success), len(quality_success) + len(quality_fail)),
                    "edited_notes": len({subject(e, notes) or e.operation_id.split(":")[0] for e in selected if e.name == "note_edited"}),
                    "processing_p95_ms": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None,
                    **{f"reminder_{state}": sum(value == state for value in deliveries.values()) for state in ("sent", "blocked", "unknown")},
                    "reminder_failed": sum(value in {"retryable", "failed"} for value in deliveries.values()),
                    "reminder_opened": len({(e.user_id, e.operation_id) for e in selected if e.name == "reminder_opened"}),
                    "costs": costs(usage, len(active_users), len(sessions), sum(row["dau"] for row in daily))},
        "generated_at": utc(now),
    }
    return AdminSummary.model_validate(summary), usage_offset + usage_limit if usage_offset + usage_limit < len(usage) else None


def safe_cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_csv(summary, filters=None):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(("section", "date_or_step", "metric", "value"))

    def flatten(section, identity, data, prefix=""):
        for name, value in data.items():
            field = prefix + name
            if isinstance(value, dict):
                flatten(section, identity, value, field + ".")
            else:
                writer.writerow([safe_cell(cell) for cell in (section, identity, field, value)])

    data = summary.model_dump()
    for section in ("cards", "retention", "quality"):
        flatten(section, "", data[section])
    for row in data["daily"]:
        flatten("daily", row["date"], {key: value for key, value in row.items() if key != "date"})
    for row in data["funnel"]:
        flatten("funnel", row["step"], {key: value for key, value in row.items() if key != "step"})
    for name, value in (filters or {}).items():
        writer.writerow([safe_cell(cell) for cell in ("filter", "", name, value)])
    writer.writerow(("meta", "", "generated_at", safe_cell(data["generated_at"])))
    return buffer.getvalue()
