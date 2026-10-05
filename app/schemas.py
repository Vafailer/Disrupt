from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NonBlank = Annotated[str, StringConstraints(min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Credentials(StrictModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-zА-Яа-яЁё0-9_.-]+$")
    password: str = Field(min_length=10, max_length=128)


class TextCapture(StrictModel):
    text: str = Field(min_length=1, max_length=12000)
    processing_mode: Literal["ai", "manual"] = "ai"

    @field_validator("text")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Text must contain non-whitespace characters and no null bytes")
        return value  # Preserve the exact original, including whitespace.


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
    search_operation_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )


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


class CategoryResponse(StrictModel):
    id: str
    name: str
    version: int


class ConclusionResponse(ProposedConclusion):
    id: str
    status: Literal["proposed", "accepted", "rejected"]


class NoteResponse(StrictModel):
    id: str
    capture_id: str
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


TelegramID = Annotated[int, Field(ge=1, le=9223372036854775807)]


class TelegramOperation(StrictModel):
    bot_id: TelegramID
    update_id: int = Field(ge=0, le=9223372036854775807)
    telegram_user_id: TelegramID
    chat_id: TelegramID


class TelegramText(TelegramOperation, TextCapture):
    pass


class TelegramLink(TelegramOperation):
    code: str = Field(min_length=40, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


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


class IntegrationError(StrictModel):
    error: dict[str, str]
    operation_id: str
