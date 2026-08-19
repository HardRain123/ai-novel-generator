from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    genre: str = ""
    target_audience: str = ""
    estimated_words: int = Field(default=100000, ge=0, le=10000000)
    writing_style: str = ""
    premise: str = ""


class WorkUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    genre: str | None = None
    target_audience: str | None = None
    estimated_words: int | None = Field(default=None, ge=0, le=10000000)
    writing_style: str | None = None
    premise: str | None = None


class StoryBibleUpdate(BaseModel):
    summary: str = ""
    theme: str = ""
    world: str = ""
    ending: str = ""
    style_rules: str = ""
    locked: bool = False


class CharacterUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: str = ""
    goal: str = ""
    conflict: str = ""
    personality: str = ""
    background: str = ""
    status: str = ""
    knowledge: str = ""


class ChapterPlanUpdate(BaseModel):
    chapter_no: int = Field(ge=1, le=10000)
    title: str = ""
    goal: str = ""
    conflict: str = ""
    beats: list[str] = []
    hook: str = ""


class ChapterUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    status: Literal["draft", "approved"] | None = None


class GenerateChapterRequest(BaseModel):
    chapter_no: int = Field(default=1, ge=1, le=10000)
    mode: Literal["plan", "chapter", "continue", "rewrite"] = "chapter"
    instruction: str = ""


class GenerateOutlineRequest(BaseModel):
    chapter_count: int = Field(default=12, ge=1, le=200)


class QualityIssue(BaseModel):
    kind: str
    severity: Literal["low", "medium", "high"]
    message: str
    evidence: str = ""
    suggestion: str = ""


class WorkResponse(BaseModel):
    id: str
    title: str
    genre: str
    target_audience: str
    estimated_words: int
    writing_style: str
    premise: str
    status: str
    created_at: str
    updated_at: str


class GenerateResult(BaseModel):
    kind: str
    data: dict[str, Any]

