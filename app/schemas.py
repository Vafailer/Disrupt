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


class StructuredNote(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=24000)
    conclusions: list[ProposedConclusion] = Field(max_length=5)

    @field_validator("title", "markdown")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value


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
