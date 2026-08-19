from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    genre: str = ""
    target_audience: str = ""
    estimated_words: int = Field(default=100000, ge=0, le=10000000)
    writing_style: str = ""
    premise: str = ""
    model_profile_id: str | None = None


class WorkUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    genre: str | None = None
    target_audience: str | None = None
    estimated_words: int | None = Field(default=None, ge=0, le=10000000)
    writing_style: str | None = None
    premise: str | None = None
    model_profile_id: str | None = None


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
    pov_character: str = ""
    opening_state: dict[str, Any] = Field(default_factory=dict)
    causal_beats: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_changes: list[dict[str, Any]] = Field(default_factory=list)
    state_changes: list[dict[str, Any]] = Field(default_factory=list)
    foreshadow_actions: list[dict[str, Any]] = Field(default_factory=list)
    forbidden_reveals: list[str] = Field(default_factory=list)
    ending_state: dict[str, Any] = Field(default_factory=dict)


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


class StateReviewItem(BaseModel):
    id: str = Field(min_length=1)
    kind: Literal["character", "timeline", "alias", "foreshadow"]
    action: Literal["accept", "reject"]
    edited_value: Any | None = None


class StateReviewRequest(BaseModel):
    items: list[StateReviewItem] = Field(min_length=1, max_length=500)


class GenerationJobCreate(BaseModel):
    kind: Literal["setup", "outline", "chapter", "state_extraction"]
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)
    model_profile_id: str | None = None


class ModelProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    provider: str = "openai_compatible"
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = ""
    reasoning_effort: Literal["auto", "low", "medium", "high", "xhigh"] = "auto"
    timeout_seconds: float = Field(default=90, gt=0, le=600)
    is_default: bool = False


class ModelProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    provider: str | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = None
    clear_api_key: bool = False
    reasoning_effort: Literal["auto", "low", "medium", "high", "xhigh"] | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    is_default: bool | None = None
    enabled: bool | None = None


class ForeshadowCreate(BaseModel):
    clue: str = Field(min_length=1, max_length=2000)
    kind: str = "clue"
    planted_chapter: int = Field(default=0, ge=0)
    expected_reveal_chapter: int = Field(default=0, ge=0)
    status: Literal["open", "revealed", "deferred", "abandoned"] = "open"
    actual_reveal_chapter: int = Field(default=0, ge=0)
    note: str = ""
    evidence: str = ""


class ForeshadowUpdate(BaseModel):
    clue: str | None = Field(default=None, min_length=1, max_length=2000)
    kind: str | None = None
    planted_chapter: int | None = Field(default=None, ge=0)
    expected_reveal_chapter: int | None = Field(default=None, ge=0)
    status: Literal["open", "revealed", "deferred", "abandoned"] | None = None
    actual_reveal_chapter: int | None = Field(default=None, ge=0)
    note: str | None = None
    evidence: str | None = None


class TrendSearchRequest(BaseModel):
    sources: list[str] = Field(default_factory=lambda: ["fanqie", "qidian", "jjwxc"])
    category: str = ""
    board: str = ""
    keyword: str = ""
    refresh: bool = False


class TrendAnalyzeRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=20)
    model_profile_id: str | None = None


class CreateFromTrendIdeaRequest(BaseModel):
    analysis_id: str
    idea_index: int = Field(default=0, ge=0, le=4)
    model_profile_id: str | None = None

