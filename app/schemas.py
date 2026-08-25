from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    genre: str = ""
    target_audience: str = ""
    estimated_words: int = Field(default=100000, ge=0, le=10000000)
    average_chapter_words: int = Field(default=2500, ge=800, le=10000)
    target_chapter_count: int | None = Field(default=None, ge=1, le=10000)
    writing_style: str = ""
    premise: str = ""
    model_profile_id: str | None = None


class WorkUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    genre: str | None = None
    target_audience: str | None = None
    estimated_words: int | None = Field(default=None, ge=0, le=10000000)
    average_chapter_words: int | None = Field(default=None, ge=800, le=10000)
    target_chapter_count: int | None = Field(default=None, ge=1, le=10000)
    writing_style: str | None = None
    premise: str | None = None
    model_profile_id: str | None = None


class StoryBibleUpdate(BaseModel):
    summary: str = ""
    theme: str = ""
    world: str = ""
    ending: str = ""
    style_rules: str = ""
    title_interpretation: str = ""
    reader_promise: str = ""
    core_hook: str = ""
    core_conflict: str = ""
    stakes: str = ""
    must_have_elements: list[str] = Field(default_factory=list)
    avoid_drift: list[str] = Field(default_factory=list)
    locked: bool = False


class CharacterUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: str = ""
    story_function: str = ""
    biography: str = ""
    dramatic_core: dict[str, str] = Field(default_factory=dict)
    appearance: str = ""
    portrayal: str = ""
    arc: str = ""
    facets: dict[str, Any] = Field(default_factory=dict)
    # Legacy fields are accepted so existing clients and saved drafts remain valid.
    goal: str = ""
    conflict: str = ""
    personality: str = ""
    background: str = ""
    status: str = ""
    knowledge: str = ""
    motivation: str = ""
    flaw: str = ""
    character_arc: str = ""
    secret: str = ""
    relationships: str = ""
    voice: str = ""


class ChapterPlanUpdate(BaseModel):
    expected_version: int | None = Field(default=None, ge=1)
    title: str | None = None
    goal: str | None = None
    conflict: str | None = None
    failure_cost: str | None = None
    beats: list[str] | None = None
    hook: str | None = None
    pov_character: str | None = None
    opening_state: dict[str, Any] | None = None
    causal_beats: list[dict[str, Any]] | None = None
    knowledge_changes: list[dict[str, Any]] | None = None
    state_changes: list[dict[str, Any]] | None = None
    foreshadow_actions: list[dict[str, Any]] | None = None
    forbidden_reveals: list[str] | None = None
    ending_state: dict[str, Any] | None = None
    appearing_characters: list[str] | None = None
    appearing_factions: list[str] | None = None
    task_progress: list[dict[str, Any]] | None = None
    plot_arc: str | None = None
    title_promise_progress: str | None = None
    character_arc_progress: str | None = None
    story_day: int | None = None
    phase_key: str | None = None
    time_mode: Literal["linear", "flashback", "parallel"] | None = None
    start_time: str | None = Field(default=None, max_length=120)
    end_time: str | None = Field(default=None, max_length=120)
    previous_chapter_no: int | None = Field(default=None, ge=1)
    calibration_status: Literal["calibrated", "pending_calibration"] | None = None
    dependencies: list[dict[str, Any]] | None = None


class StoryPhaseUpdate(BaseModel):
    phase_key: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    start_day: int | None = None
    end_day: int | None = None
    rules: list[str] = Field(default_factory=list)
    allowed: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    transition_conditions: list[str] = Field(default_factory=list)
    locked: bool = False


class FactionUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    precursor_name: str = ""
    lifecycle: str = "planned"
    prepared_day: int | None = None
    formed_day: int | None = None
    public_day: int | None = None
    active_from_day: int | None = None
    dissolved_day: int | None = None
    first_appearance_chapter: int = Field(default=0, ge=0)
    description: str = ""
    state: dict[str, Any] = Field(default_factory=dict)


class StoryGoalCreate(BaseModel):
    owner_type: str = "character"
    owner_id: str | None = None
    title: str = Field(min_length=1, max_length=240)
    status: str = "planned"
    priority: int = 0
    started_day: int | None = None
    ended_day: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class StoryGoalUpdate(BaseModel):
    status: str | None = None
    priority: int | None = None
    started_day: int | None = None
    ended_day: int | None = None
    details: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    chapter_no: int = Field(default=0, ge=0)
    story_day: int | None = None
    evidence: str = ""


class LongTermFactUpsert(BaseModel):
    entity_type: str = Field(min_length=1, max_length=80)
    entity_id: str | None = Field(default=None, max_length=120)
    fact_key: str = Field(min_length=1, max_length=120)
    value: dict[str, Any] = Field(default_factory=dict)
    source: str = "author"
    locked: bool = False


class FuturePlanCreate(BaseModel):
    entity_type: str = Field(min_length=1, max_length=80)
    entity_id: str | None = Field(default=None, max_length=120)
    plan_type: str = Field(default="goal", min_length=1, max_length=80)
    target_chapter: int | None = Field(default=None, ge=1)
    content: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"


class FuturePlanUpdate(BaseModel):
    entity_type: str | None = Field(default=None, min_length=1, max_length=80)
    entity_id: str | None = Field(default=None, max_length=120)
    plan_type: str | None = Field(default=None, min_length=1, max_length=80)
    target_chapter: int | None = Field(default=None, ge=1)
    content: dict[str, Any] | None = None
    status: str | None = None


class ChapterUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    status: Literal["draft", "approved"] | None = None


class GenerateChapterRequest(BaseModel):
    chapter_no: int = Field(default=1, ge=1, le=10000)
    mode: Literal["plan", "chapter", "continue", "rewrite"] = "chapter"
    instruction: str = ""


class GenerateOutlineRequest(BaseModel):
    # chapter_count remains accepted for older clients.  New clients use the
    # total target and a short, explicit generation range.
    chapter_count: int | None = Field(default=None, ge=1, le=10000)
    total_target_chapters: int | None = Field(default=None, ge=1, le=10000)
    mode: Literal["initial", "replan", "extend"] = "initial"
    from_chapter: int = Field(default=1, ge=1, le=10000)
    to_chapter: int | None = Field(default=None, ge=1, le=10000)
    expected_outline_version: int | None = Field(default=None, ge=0)
    expected_fact_version: int | None = Field(default=None, ge=0)


class StoryVolumeUpdate(BaseModel):
    sequence: int = Field(ge=1, le=1000)
    title: str = Field(min_length=1, max_length=160)
    start_chapter: int = Field(ge=1, le=10000)
    end_chapter: int = Field(ge=1, le=10000)
    target_words: int = Field(default=0, ge=0, le=10000000)
    synopsis: str = ""
    goal: str = ""
    opposition: str = ""
    ending_state: dict[str, Any] = Field(default_factory=dict)
    status: str = "planned"


class NarrativeStageUpdate(BaseModel):
    volume_id: str = Field(min_length=1)
    sequence: int = Field(ge=1, le=100)
    title: str = Field(min_length=1, max_length=160)
    start_chapter: int = Field(ge=1, le=10000)
    end_chapter: int = Field(ge=1, le=10000)
    purpose: str = ""
    entry_state: dict[str, Any] = Field(default_factory=dict)
    exit_state: dict[str, Any] = Field(default_factory=dict)
    allowed_payoffs: list[str] = Field(default_factory=list)
    forbidden_payoffs: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    status: str = "planned"


class NarrativeStructureBootstrap(BaseModel):
    replace: bool = False


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


class ChapterConfirmRequest(BaseModel):
    extraction_id: str = Field(min_length=1)
    items: list[StateReviewItem] = Field(min_length=1, max_length=500)


class EventRollbackRequest(BaseModel):
    reason: str = Field(default="作者回滚事件", min_length=1, max_length=1000)


class GenerationJobCreate(BaseModel):
    kind: Literal["setup", "outline", "volume_outline", "chapter", "state_extraction", "planning_step", "planning_character_batch"]
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)
    model_profile_id: str | None = None


class PlanningStepGenerateRequest(BaseModel):
    item_key: str = "default"
    feedback: str = Field(default="", max_length=4000)
    preset: str | None = None
    candidate_count: int = Field(default=1, ge=1, le=3)
    model_profile_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)


class PlanningCharacterBatchRequest(BaseModel):
    """Generate biographies for roster characters that do not yet have a draft."""

    feedback: str = Field(default="", max_length=4000)
    preset: str | None = None
    model_profile_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)


class PlanningArtifactUpdate(BaseModel):
    content: dict[str, Any] = Field(default_factory=dict)
    feedback: str = Field(default="", max_length=4000)


class PlanningArtifactConfirm(BaseModel):
    candidate_index: int | None = Field(default=None, ge=0, le=2)


class PlanningResetRequest(BaseModel):
    confirm: bool = False


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


class ProxySettingsUpdate(BaseModel):
    enabled: bool = False
    port: int = Field(default=10808, ge=1, le=65535)


class PromptSettingUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=50000)


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
    idempotency_key: str | None = Field(default=None, max_length=200)


class CreateFromInspirationBlueprintRequest(BaseModel):
    blueprint_id: str
    model_profile_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)

