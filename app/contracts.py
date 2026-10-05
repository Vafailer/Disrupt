"""Integration-v1 schemas for upcoming endpoints, published before the UI/adapter.

These types do not register placeholder runtime routes. Read docs/integration-v1.md
for implemented vs contract-only methods.
"""

from typing import Literal

from pydantic import Field, model_validator

from app.schemas import StrictModel, TelegramID


class ActionRequest(StrictModel):
    bot_id: TelegramID
    update_id: int = Field(ge=0, le=9223372036854775807)
    telegram_user_id: TelegramID
    callback_token: str = Field(min_length=32, max_length=64)


class ActionResponse(StrictModel):
    status: Literal["completed", "already_completed"]


class ClaimRequest(StrictModel):
    bot_id: TelegramID
    limit: int = Field(ge=1, le=100)


class Delivery(StrictModel):
    delivery_id: str
    lease_token: str
    generation: int = Field(ge=1)
    chat_id: TelegramID
    text: str
    note_url: str
    callback_token: str | None


class ClaimResponse(StrictModel):
    items: list[Delivery]


class AuthorizeRequest(StrictModel):
    lease_token: str = Field(min_length=32, max_length=100)
    generation: int = Field(ge=1)


class AuthorizeResponse(StrictModel):
    send: bool


class ResultRequest(AuthorizeRequest):
    status: Literal["sent", "blocked", "retryable", "unknown"]
    telegram_message_id: TelegramID | None
    error_code: str | None = Field(max_length=64)
    retry_after_seconds: int | None = Field(ge=0, le=86400)


class ResultResponse(StrictModel):
    status: Literal["recorded"]


class Percentage(StrictModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None = Field(ge=0, le=100)

    @model_validator(mode="after")
    def valid_percentage(self):
        if self.numerator > self.denominator:
            raise ValueError("Numerator exceeds denominator")
        if self.denominator == 0:
            if self.value is not None:
                raise ValueError("No denominator means unknown percentage")
        elif self.value is None or abs(self.value - 100 * self.numerator / self.denominator) > 0.000001:
            raise ValueError("Percentage must match its counts")
        return self


class Cards(StrictModel):
    registrations: int
    dau: int
    unique_users: int
    new_users: int
    returning_users: int
    ai_activation: Percentage
    manual_activation: Percentage
    activation_pending: int
    completed_scenario: int
    returns: int
    d1: Percentage
    d7: Percentage


class DailyMetrics(StrictModel):
    date: str
    registrations: int
    dau: int
    new_users: int
    returning_users: int
    llm_cost: str | None
    stt_cost: str | None
    unknown_usage_calls: int
    session_count: int
    llm_cost_per_dau: str | None


class FunnelStep(StrictModel):
    step: Literal["registered", "capture_saved", "note_opened", "structure_checked", "returned"]
    users: int
    conversion: Percentage


class Retention(StrictModel):
    d1: Percentage
    d7: Percentage
    pending_d1: int
    pending_d7: int


class UsageRow(StrictModel):
    id: str
    user_pseudonym: str
    session_pseudonym: str | None
    operation_pseudonym: str
    occurred_at: str
    channel: Literal["web", "telegram"]
    kind: Literal["llm", "stt"]
    model: str
    tariff_version: str | None
    status: str
    input_tokens: int | None
    output_tokens: int | None
    cache_tokens: int | None
    stt_seconds: float | None
    cost: str | None
    estimated_cost: str | None
    latency_ms: int | None


class Costs(StrictModel):
    currency: Literal["RUB"]
    llm_cost: str | None
    stt_cost: str | None
    known_llm_cost: str
    known_stt_cost: str
    unknown_usage_calls: int
    llm_calls: int
    stt_calls: int
    input_tokens: int | None
    output_tokens: int | None
    cache_tokens: int | None
    stt_minutes: str | None
    user_count: int
    session_count: int
    dau_sum: int
    cost_per_user: str | None
    cost_per_session: str | None
    llm_cost_per_dau: str | None


class Quality(StrictModel):
    ai_succeeded: int
    ai_failed: int
    ai_success_rate: Percentage
    edited_notes: int
    processing_p95_ms: int | None
    reminder_sent: int
    reminder_blocked: int
    reminder_failed: int
    reminder_unknown: int
    reminder_opened: int
    costs: Costs


class AdminSummary(StrictModel):
    cards: Cards
    daily: list[DailyMetrics]
    funnel: list[FunnelStep]
    retention: Retention
    usage: list[UsageRow]
    quality: Quality
    generated_at: str
