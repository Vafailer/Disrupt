import re
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NonBlank = Annotated[str, StringConstraints(min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Credentials(StrictModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=128)


class Registration(Credentials):
    # Consent is checked in the route so that the message can say what is missing.
    accept_policy: bool = False
    policy_version: str | None = Field(default=None, max_length=32)
    confirm_age: bool = False


class TelegramLoginStart(StrictModel):
    accept_policy: bool = False
    policy_version: str | None = Field(default=None, max_length=32)
    confirm_age: bool = False


class RecoveryRequest(StrictModel):
    # No username pattern: every input gets the same answer, so older accounts are not singled out.
    username: str = Field(min_length=1, max_length=64)


class PasswordReset(StrictModel):
    token: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=10, max_length=128)


class TokenBody(StrictModel):
    token: str = Field(min_length=1, max_length=200)


class AcceptPolicy(StrictModel):
    policy_version: str = Field(min_length=1, max_length=32)


class DeletionRequest(StrictModel):
    password: str = Field(min_length=1, max_length=128)


EMAIL_PATTERN = re.compile(r"[^@\s\x00-\x1f]{1,64}@[^@\s\x00-\x1f]+\.[^@\s\x00-\x1f]{2,}")


class EmailBody(StrictModel):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.strip().lower()
        if len(value) > 254 or not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("Invalid email address")
        return value


class EmailCredentials(EmailBody):
    password: str = Field(min_length=10, max_length=128)


class EmailRegistrationStart(EmailCredentials):
    accept_policy: bool = False
    policy_version: str | None = Field(default=None, max_length=32)
    confirm_age: bool = False


class EmailRegistrationConfirm(StrictModel):
    registration_id: str = Field(min_length=36, max_length=36)
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class TextCapture(StrictModel):
    text: str = Field(min_length=1, max_length=12000)
    processing_mode: Literal["ai", "manual"] = "ai"

    @field_validator("text")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Text must contain non-whitespace characters and no null bytes")
        return value  # Preserve the exact original, including whitespace.


class AudioJobResponse(StrictModel):
    id: str
    capture_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    provider: Literal["mock", "cloudru"]
    error_code: str | None
    note_id: str | None
    original_text: str
    created_at: float
    finished_at: float | None


class AudioMetadata(StrictModel):
    input_kind: Literal["text", "audio"] = "text"
    transcript: str | None = None
    transcript_version: int = 1
    transcript_origin: Literal["stt", "user", "legacy"] | None = None
    audio_seconds: float | None = None
    audio_media_type: str | None = None


class CaptureResponse(AudioMetadata):
    capture_id: str
    original_text: str
    processing_mode: Literal["ai", "manual"]
    note_id: str | None
    job: AudioJobResponse | None


class TranscriptEdit(StrictModel):
    version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=12000)

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty transcript or null bytes")
        return value


class TranscriptRevisionResponse(StrictModel):
    version: int
    text: str
    origin: Literal["stt", "user", "legacy"]
    created_at: float | None


class ProposedConclusion(StrictModel):
    text: str = Field(min_length=1, max_length=1500)
    source_quote: str = Field(min_length=1, max_length=3000)

    @field_validator("text", "source_quote")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value


ItemKind = Literal["note", "idea", "task", "goal", "plan"]


class ProposedItem(ProposedConclusion):
    kind: ItemKind
    due_text: str | None = Field(default=None, max_length=300)


class StructuredNote(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=24000)
    conclusions: list[ProposedConclusion] = Field(max_length=5)
    items: list[ProposedItem] = Field(default_factory=list, max_length=30)
    category_name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("title", "markdown")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value

    @field_validator("category_name")
    @classmethod
    def valid_category(cls, value):
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("Empty category or null bytes")
        return value.strip() if value is not None else None


class NoteEdit(StrictModel):
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=24000)

    @field_validator("title", "markdown")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value


class ConclusionEdit(StrictModel):
    version: int = Field(ge=1)
    status: Literal["accepted", "rejected"]


class VersionRequest(StrictModel):
    version: int = Field(ge=1)


class ReminderContent(StrictModel):
    scheduled_at: str = Field(min_length=20, max_length=40)
    timezone: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=1000)

    @field_validator("scheduled_at")
    @classmethod
    def absolute_time(cls, value):
        date = datetime.fromisoformat(value)
        if "T" not in value or date.tzinfo is None or date.utcoffset() is None:
            raise ValueError("An absolute ISO timestamp with an offset is required")
        date.timestamp()
        return value

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown IANA timezone") from None
        return value

    @field_validator("text")
    @classmethod
    def reminder_text(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value


class ReminderCreate(ReminderContent):
    item_id: str | None = Field(default=None, min_length=1, max_length=36)


class ReminderEdit(ReminderContent):
    generation: int = Field(ge=1)


class ReminderCancel(StrictModel):
    generation: int = Field(ge=1)


class ReminderResponse(StrictModel):
    id: str
    note_id: str
    item_id: str | None
    scheduled_at: str
    timezone: str
    text: str
    status: str
    generation: int
    confirmed_at: str
    delivery_status: str | None
    previous_attempt_unknown: bool


class ReminderTimeRequest(StrictModel):
    local_time: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?$")
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        return ReminderContent.known_timezone(value)


class ReminderTimeChoice(StrictModel):
    scheduled_at: str
    local_at: str
    utc_offset: str
    is_future: bool


class ReminderTimeResponse(StrictModel):
    local_time: str
    timezone: str
    ambiguous: bool
    choices: list[ReminderTimeChoice]


class ItemCreate(VersionRequest):
    kind: ItemKind
    text: str = Field(min_length=1, max_length=1500)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value


class ItemEdit(ItemCreate):
    status: Literal["open", "completed"]


class CategoryCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty category or null bytes")
        return value.strip()


class CategoryEdit(CategoryCreate, VersionRequest):
    pass


class NoteCategoryEdit(VersionRequest):
    category_id: str | None = Field(max_length=36)


class UserAction(StrictModel):
    operation_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class NoteOpened(UserAction):
    reminder_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    search_operation_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )


class ProposedReminder(StrictModel):
    local_time: str
    timezone: str
    label: str


class ItemResponse(StrictModel):
    id: str
    note_id: str
    kind: ItemKind
    text: str
    status: Literal["open", "completed"]
    version: int
    source_quote: str | None
    due_text: str | None
    due_at: float | None
    proposed_reminder: ProposedReminder | None = None


class CategoryResponse(StrictModel):
    id: str
    name: str
    version: int


class ConclusionResponse(ProposedConclusion):
    id: str
    status: Literal["proposed", "accepted", "rejected"]


class NoteResponse(AudioMetadata):
    id: str
    capture_id: str
    channel: Literal["web", "telegram"]
    original_text: str
    title: str
    markdown: str
    conclusions: list[ConclusionResponse]
    items: list[ItemResponse]
    category_id: str | None
    category_name: str | None
    structure_confirmed_at: float | None
    version: int
    provider: str
    created_at: float
    updated_at: float


class NoteSummary(StrictModel):
    id: str
    title: str
    version: int
    updated_at: float
    category_id: str | None
    channel: Literal["web", "telegram"]
    input_kind: Literal["text", "audio"]


TelegramID = Annotated[int, Field(ge=1, le=9223372036854775807)]


class TelegramOperation(StrictModel):
    bot_id: TelegramID
    update_id: int = Field(ge=0, le=9223372036854775807)
    telegram_user_id: TelegramID
    chat_id: TelegramID


class TelegramText(TelegramOperation, TextCapture):
    pass


class TelegramLoginConfirm(TelegramOperation):
    token: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")
    telegram_username: str | None = Field(default=None, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    first_name: str | None = Field(default=None, max_length=128)


class TelegramLink(TelegramOperation):
    code: str = Field(min_length=22, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class LinkCodeResponse(StrictModel):
    link_request_id: str
    code: str
    expires_at: str


class LinkResponse(StrictModel):
    link_request_id: str
    status: Literal["pending"]


class TelegramCaptureResponse(StrictModel):
    capture_id: str
    job_id: str | None
    status: Literal["saved"]
    note_url: str
    ai_limit_exceeded: bool = False
    ai_units_remaining: int | None = None
    ai_limit_resets_at: str | None = None


class IntegrationErrorDetail(StrictModel):
    code: Literal[
        "unauthorized", "forbidden", "not_found", "conflict", "invalid_input",
        "rate_limited", "unavailable", "internal_error", "telegram_not_linked",
        "link_expired", "link_conflict", "invalid_link", "input_too_large",
        "unsupported_audio", "empty_input", "action_forbidden", "action_expired", "quota_exceeded",
    ]
    message: str


class IntegrationError(StrictModel):
    error: IntegrationErrorDetail
    operation_id: str
