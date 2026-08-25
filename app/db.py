import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

from app.config import DATABASE_URL
from app.utils import json_dumps, json_loads, now_iso


_lock = threading.RLock()


def _sqlite_path(database_url: str) -> str:
    if database_url in {"sqlite:///:memory:", ":memory:"}:
        return ":memory:"
    if database_url.startswith("sqlite:///"):
        return unquote(database_url[len("sqlite:///") :])
    return database_url or "data.db"


def get_conn() -> sqlite3.Connection:
    path = _sqlite_path(DATABASE_URL)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def transaction():
    with _lock:
        conn = get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    if not table_exists:
        return
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_state_extraction_schema(conn: sqlite3.Connection) -> None:
    """为已有 MVP 数据库补齐 Phase 1 字段；新库和旧库都可启动。"""
    _ensure_column(conn, "character_states", "as_of_chapter", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "character_states", "source_version_id", "TEXT")
    for column, definition in {
        "pov_character": "TEXT NOT NULL DEFAULT ''",
        "opening_state_json": "TEXT NOT NULL DEFAULT '{}'",
        "causal_beats_json": "TEXT NOT NULL DEFAULT '[]'",
        "knowledge_changes_json": "TEXT NOT NULL DEFAULT '[]'",
        "state_changes_json": "TEXT NOT NULL DEFAULT '[]'",
        "foreshadow_actions_json": "TEXT NOT NULL DEFAULT '[]'",
        "forbidden_reveals_json": "TEXT NOT NULL DEFAULT '[]'",
        "ending_state_json": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        _ensure_column(conn, "chapter_plans", column, definition)
    for column, definition in {
        "run_no": "INTEGER NOT NULL DEFAULT 1",
        "prompt_version": "TEXT NOT NULL DEFAULT 'v1'",
        "error": "TEXT NOT NULL DEFAULT ''",
        "superseded_by": "TEXT",
    }.items():
        _ensure_column(conn, "state_extractions", column, definition)
    for column, definition in {
        "reviewed_value_json": "TEXT",
        "reviewed_at": "TEXT",
    }.items():
        _ensure_column(conn, "character_state_changes", column, definition)
    for column, definition in {
        "story_time_text": "TEXT NOT NULL DEFAULT ''",
        "time_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "location": "TEXT NOT NULL DEFAULT ''",
        "participants_json": "TEXT NOT NULL DEFAULT '[]'",
        "evidence": "TEXT NOT NULL DEFAULT ''",
        "confidence": "REAL NOT NULL DEFAULT 0",
        "review_status": "TEXT NOT NULL DEFAULT 'pending'",
        "source_extraction_id": "TEXT",
    }.items():
        _ensure_column(conn, "timeline_events", column, definition)
    _ensure_column(conn, "works", "model_profile_id", "TEXT")
    for column, definition in {
        "kind": "TEXT NOT NULL DEFAULT 'clue'",
        "actual_reveal_chapter": "INTEGER NOT NULL DEFAULT 0",
        "note": "TEXT NOT NULL DEFAULT ''",
        "evidence": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        _ensure_column(conn, "foreshadows", column, definition)
    for column, definition in {
        "stage": "TEXT NOT NULL DEFAULT 'queued'",
        "stage_label": "TEXT NOT NULL DEFAULT '排队中'",
        "message": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
        "model_profile_id": "TEXT",
        "cancel_requested_at": "TEXT",
        "input_tokens": "INTEGER",
        "output_tokens": "INTEGER",
        "total_tokens": "INTEGER",
        "resolved_provider": "TEXT NOT NULL DEFAULT ''",
        "resolved_model": "TEXT NOT NULL DEFAULT ''",
        "resolved_base_url": "TEXT NOT NULL DEFAULT ''",
        "model_started_at": "TEXT",
        "model_first_output_at": "TEXT",
        "metrics_json": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        _ensure_column(conn, "generation_jobs", column, definition)

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chapter_versions (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            chapter_id TEXT NOT NULL,
            chapter_no INTEGER NOT NULL,
            version_no INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            UNIQUE(work_id, chapter_no, version_no),
            UNIQUE(work_id, chapter_no, content_hash),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS state_extractions (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            chapter_version_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            extractor_version TEXT NOT NULL DEFAULT 'v1',
            run_no INTEGER NOT NULL DEFAULT 1,
            prompt_version TEXT NOT NULL DEFAULT 'v1',
            model TEXT NOT NULL DEFAULT 'fallback',
            raw_json TEXT NOT NULL DEFAULT '{}',
            warning TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            superseded_by TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(chapter_version_id, extractor_version),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (chapter_version_id) REFERENCES chapter_versions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS character_state_changes (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            extraction_id TEXT NOT NULL,
            character_id TEXT,
            character_name TEXT NOT NULL,
            field TEXT NOT NULL,
            old_value_json TEXT,
            new_value_json TEXT,
            reviewed_value_json TEXT,
            evidence TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (extraction_id) REFERENCES state_extractions(id) ON DELETE CASCADE,
            FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS character_alias_candidates (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            extraction_id TEXT NOT NULL,
            character_id TEXT,
            character_name TEXT NOT NULL,
            alias TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            UNIQUE(extraction_id, alias),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (extraction_id) REFERENCES state_extractions(id) ON DELETE CASCADE,
            FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE SET NULL
        );
            CREATE TABLE IF NOT EXISTS character_aliases (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            alias TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'extraction',
            created_at TEXT NOT NULL,
            UNIQUE(work_id, alias),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS state_rebuild_jobs (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            from_chapter INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        """
    )


def _migrate_story_planning_schema(conn: sqlite3.Connection) -> None:
    """补齐书名契约、人物小传和大纲承诺字段。"""
    for column, definition in {
        "title_interpretation": "TEXT NOT NULL DEFAULT ''",
        "reader_promise": "TEXT NOT NULL DEFAULT ''",
        "core_hook": "TEXT NOT NULL DEFAULT ''",
        "core_conflict": "TEXT NOT NULL DEFAULT ''",
        "stakes": "TEXT NOT NULL DEFAULT ''",
        "must_have_elements_json": "TEXT NOT NULL DEFAULT '[]'",
        "avoid_drift_json": "TEXT NOT NULL DEFAULT '[]'",
        "generation_source": "TEXT NOT NULL DEFAULT ''",
        "quality_score": "INTEGER NOT NULL DEFAULT 0",
        "quality_issues_json": "TEXT NOT NULL DEFAULT '[]'",
    }.items():
        _ensure_column(conn, "story_bibles", column, definition)
    for column, definition in {
        "story_function": "TEXT NOT NULL DEFAULT ''",
        "appearance": "TEXT NOT NULL DEFAULT ''",
        "portrayal": "TEXT NOT NULL DEFAULT ''",
        "facets_json": "TEXT NOT NULL DEFAULT '{}'",
        "biography": "TEXT NOT NULL DEFAULT ''",
        "motivation": "TEXT NOT NULL DEFAULT ''",
        "flaw": "TEXT NOT NULL DEFAULT ''",
        "character_arc": "TEXT NOT NULL DEFAULT ''",
        "secret": "TEXT NOT NULL DEFAULT ''",
        "relationships": "TEXT NOT NULL DEFAULT ''",
        "voice": "TEXT NOT NULL DEFAULT ''",
        "active": "INTEGER NOT NULL DEFAULT 1",
    }.items():
        _ensure_column(conn, "characters", column, definition)
    _ensure_column(conn, "plot_arcs", "active", "INTEGER NOT NULL DEFAULT 1")
    for column, definition in {
        "plot_arc": "TEXT NOT NULL DEFAULT ''",
        "title_promise_progress": "TEXT NOT NULL DEFAULT ''",
        "character_arc_progress": "TEXT NOT NULL DEFAULT ''",
    }.items():
        _ensure_column(conn, "chapter_plans", column, definition)

    for column, definition in {
        "status": "TEXT NOT NULL DEFAULT 'in_progress'",
        "current_step": "TEXT NOT NULL DEFAULT 'contract'",
        "preset": "TEXT NOT NULL DEFAULT 'custom'",
        "input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "output_tokens": "INTEGER NOT NULL DEFAULT 0",
        "total_tokens": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        _ensure_column(conn, "planning_sessions", column, definition)
    for column, definition in {
        "step": "TEXT NOT NULL DEFAULT ''",
        "item_key": "TEXT NOT NULL DEFAULT ''",
        "content_json": "TEXT NOT NULL DEFAULT '{}'",
        "status": "TEXT NOT NULL DEFAULT 'draft'",
        "version": "INTEGER NOT NULL DEFAULT 1",
        "source": "TEXT NOT NULL DEFAULT 'model'",
        "feedback": "TEXT NOT NULL DEFAULT ''",
        "checks_json": "TEXT NOT NULL DEFAULT '{}'",
        "parent_versions_json": "TEXT NOT NULL DEFAULT '{}'",
        "input_tokens": "INTEGER",
        "output_tokens": "INTEGER",
        "total_tokens": "INTEGER",
        "model": "TEXT NOT NULL DEFAULT ''",
        "confirmed_at": "TEXT",
    }.items():
        _ensure_column(conn, "planning_artifacts", column, definition)

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS planning_sessions (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'in_progress',
            current_step TEXT NOT NULL DEFAULT 'contract',
            preset TEXT NOT NULL DEFAULT 'custom',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS planning_artifacts (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            step TEXT NOT NULL,
            item_key TEXT NOT NULL,
            content_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'draft',
            version INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'model',
            feedback TEXT NOT NULL DEFAULT '',
            checks_json TEXT NOT NULL DEFAULT '{}',
            parent_versions_json TEXT NOT NULL DEFAULT '{}',
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            model TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            confirmed_at TEXT,
            UNIQUE(session_id, step, item_key),
            FOREIGN KEY (session_id) REFERENCES planning_sessions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_planning_artifacts_session ON planning_artifacts(session_id, step, status);
        CREATE TABLE IF NOT EXISTS planning_artifact_snapshots (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            step TEXT NOT NULL,
            item_key TEXT NOT NULL,
            content_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'draft',
            version INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'model',
            feedback TEXT NOT NULL DEFAULT '',
            checks_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES planning_sessions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_planning_snapshots_artifact
            ON planning_artifact_snapshots(session_id, step, item_key, created_at DESC);
        """
    )


def _cleanup_legacy_character_drafts(conn: sqlite3.Connection) -> None:
    """Drop compatibility duplicates from editable planning drafts without changing story facts."""
    from app.services.character_cards import planning_character
    from app.services.planning_quality import planning_checks

    rows = conn.execute(
        "SELECT id, content_json FROM planning_artifacts WHERE step='character' AND status='draft'"
    ).fetchall()
    for row in rows:
        content = json_loads(row["content_json"], {})
        character = content.get("character") if isinstance(content, dict) else None
        if not isinstance(character, dict):
            continue
        canonical = planning_character(character)
        if character == canonical:
            continue
        updated = dict(content)
        updated["character"] = canonical
        checks = planning_checks("character", updated)
        conn.execute(
            "UPDATE planning_artifacts SET content_json=?, checks_json=?, updated_at=? WHERE id=?",
            (json_dumps(updated), json_dumps(checks), now_iso(), row["id"]),
        )


def _migrate_state_engine_schema(conn: sqlite3.Connection) -> None:
    """Phase 2/V2: versioned story time, entity events, snapshots and audit history."""
    _ensure_column(conn, "works", "fact_version", "INTEGER NOT NULL DEFAULT 0")
    for column, definition in {
        "story_day": "INTEGER",
        "phase_key": "TEXT NOT NULL DEFAULT ''",
        "failure_cost": "TEXT NOT NULL DEFAULT ''",
        "appearing_characters_json": "TEXT NOT NULL DEFAULT '[]'",
        "appearing_factions_json": "TEXT NOT NULL DEFAULT '[]'",
        "task_progress_json": "TEXT NOT NULL DEFAULT '[]'",
        "time_mode": "TEXT NOT NULL DEFAULT 'linear'",
        "start_time": "TEXT NOT NULL DEFAULT ''",
        "end_time": "TEXT NOT NULL DEFAULT ''",
        "previous_chapter_no": "INTEGER",
        "fact_version": "INTEGER NOT NULL DEFAULT 0",
        "outline_version": "INTEGER NOT NULL DEFAULT 1",
        "calibration_status": "TEXT NOT NULL DEFAULT 'calibrated'",
        "version": "INTEGER NOT NULL DEFAULT 1",
        "dependencies_json": "TEXT NOT NULL DEFAULT '[]'",
        "stale_reason": "TEXT NOT NULL DEFAULT ''",
    }.items():
        _ensure_column(conn, "chapter_plans", column, definition)
    for column, definition in {
        "source_plan_version": "INTEGER NOT NULL DEFAULT 0",
        "stale_reason": "TEXT NOT NULL DEFAULT ''",
        "current_version_id": "TEXT",
        "state_status": "TEXT NOT NULL DEFAULT 'draft'",
    }.items():
        _ensure_column(conn, "chapters", column, definition)
    for column, definition in {
        "superseded_by": "TEXT",
        "is_current": "INTEGER NOT NULL DEFAULT 1",
        "replaced_at": "TEXT",
        "fact_version": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        _ensure_column(conn, "chapter_versions", column, definition)
    for column, definition in {
        "fact_version": "INTEGER NOT NULL DEFAULT 0",
        "source_fact_version": "INTEGER NOT NULL DEFAULT 0",
        "confidence": "REAL NOT NULL DEFAULT 1",
        "risk_level": "TEXT NOT NULL DEFAULT 'low'",
        "reversal_of": "TEXT",
        "replaced_by": "TEXT",
        "reviewed_at": "TEXT",
    }.items():
        _ensure_column(conn, "story_events", column, definition)
    for column, definition in {
        "allowed_json": "TEXT NOT NULL DEFAULT '[]'",
        "forbidden_json": "TEXT NOT NULL DEFAULT '[]'",
        "transition_conditions_json": "TEXT NOT NULL DEFAULT '[]'",
    }.items():
        _ensure_column(conn, "story_phases", column, definition)
    for column, definition in {
        "prepared_day": "INTEGER",
        "public_day": "INTEGER",
        "active_from_day": "INTEGER",
        "dissolved_day": "INTEGER",
        "locked": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        _ensure_column(conn, "factions", column, definition)
    for column, definition in {
        "progress_json": "TEXT NOT NULL DEFAULT '{}'",
        "start_event_id": "TEXT",
        "end_event_id": "TEXT",
    }.items():
        _ensure_column(conn, "story_goals", column, definition)
    _ensure_column(conn, "characters", "dynamic_scope", "TEXT NOT NULL DEFAULT 'legacy_unscoped'")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS story_phases (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            phase_key TEXT NOT NULL,
            name TEXT NOT NULL,
            start_day INTEGER,
            end_day INTEGER,
            rules_json TEXT NOT NULL DEFAULT '[]',
            allowed_json TEXT NOT NULL DEFAULT '[]',
            forbidden_json TEXT NOT NULL DEFAULT '[]',
            transition_conditions_json TEXT NOT NULL DEFAULT '[]',
            locked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(work_id, phase_key),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS factions (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            name TEXT NOT NULL,
            precursor_name TEXT NOT NULL DEFAULT '',
            lifecycle TEXT NOT NULL DEFAULT 'planned',
            formed_day INTEGER,
            first_appearance_chapter INTEGER NOT NULL DEFAULT 0,
            leader_character_id TEXT,
            description TEXT NOT NULL DEFAULT '',
            state_json TEXT NOT NULL DEFAULT '{}',
            prepared_day INTEGER,
            public_day INTEGER,
            active_from_day INTEGER,
            dissolved_day INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(work_id, name),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (leader_character_id) REFERENCES characters(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS faction_memberships (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            faction_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT '',
            joined_day INTEGER,
            left_day INTEGER,
            source_event_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (faction_id) REFERENCES factions(id) ON DELETE CASCADE,
            FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS story_goals (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            owner_type TEXT NOT NULL DEFAULT 'character',
            owner_id TEXT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'planned',
            priority INTEGER NOT NULL DEFAULT 0,
            started_day INTEGER,
            ended_day INTEGER,
            parent_goal_id TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            progress_json TEXT NOT NULL DEFAULT '{}',
            source_event_id TEXT,
            start_event_id TEXT,
            end_event_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_goal_id) REFERENCES story_goals(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS story_events (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            chapter_no INTEGER NOT NULL DEFAULT 0,
            chapter_version_id TEXT,
            story_day INTEGER,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT '',
            entity_id TEXT,
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            evidence TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'confirmed',
            superseded_by TEXT,
            fact_version INTEGER NOT NULL DEFAULT 0,
            source_fact_version INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 1,
            risk_level TEXT NOT NULL DEFAULT 'low',
            reversal_of TEXT,
            replaced_by TEXT,
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (chapter_version_id) REFERENCES chapter_versions(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_story_events_work_chapter ON story_events(work_id, chapter_no, status);
        CREATE TABLE IF NOT EXISTS fact_versions (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            version_no INTEGER NOT NULL,
            as_of_chapter INTEGER NOT NULL DEFAULT 0,
            source_event_id TEXT,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(work_id, version_no),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS story_snapshots (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            chapter_no INTEGER NOT NULL,
            fact_version INTEGER NOT NULL,
            state_json TEXT NOT NULL DEFAULT '{}',
            source_event_id TEXT,
            valid INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(work_id, chapter_no, fact_version),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_story_snapshots_lookup ON story_snapshots(work_id, chapter_no, fact_version, valid);
        CREATE TABLE IF NOT EXISTS outline_versions (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            version_no INTEGER NOT NULL,
            mode TEXT NOT NULL DEFAULT 'initial',
            from_chapter INTEGER NOT NULL DEFAULT 1,
            to_chapter INTEGER NOT NULL DEFAULT 0,
            fact_version INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            request_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(work_id, version_no),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS chapter_plan_versions (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            chapter_no INTEGER NOT NULL,
            outline_version_id TEXT NOT NULL,
            plan_version INTEGER NOT NULL,
            content_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            superseded_by TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(work_id, chapter_no, plan_version, outline_version_id),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (outline_version_id) REFERENCES outline_versions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_chapter_plan_versions_lookup ON chapter_plan_versions(work_id, chapter_no, created_at);
        CREATE TABLE IF NOT EXISTS context_audits (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            chapter_no INTEGER NOT NULL,
            purpose TEXT NOT NULL DEFAULT 'chapter',
            fact_version INTEGER NOT NULL DEFAULT 0,
            outline_version INTEGER NOT NULL DEFAULT 0,
            context_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_context_audits_lookup ON context_audits(work_id, chapter_no, purpose, created_at);
        CREATE TABLE IF NOT EXISTS long_term_facts (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            fact_key TEXT NOT NULL,
            value_json TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT 'author',
            locked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(work_id, entity_type, entity_id, fact_key),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS future_plans (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            plan_type TEXT NOT NULL DEFAULT 'goal',
            target_chapter INTEGER,
            content_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS plan_dependencies (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            chapter_no INTEGER NOT NULL,
            dependency_type TEXT NOT NULL,
            dependency_key TEXT NOT NULL,
            expected_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(work_id, chapter_no, dependency_type, dependency_key),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        """
    )

    # SQLite can keep an old schema cache for the first migration pass of an
    # existing database.  The next application start retries the non-critical
    # index, while the new columns/tables remain available immediately.
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_story_events_fact_version ON story_events(work_id, fact_version, chapter_no, status)")
    except sqlite3.OperationalError as exc:
        if "no such column" not in str(exc).lower():
            raise
    conn.execute("UPDATE characters SET dynamic_scope='legacy_unscoped' WHERE dynamic_scope IS NULL OR dynamic_scope=''" )
    conn.execute(
        "UPDATE chapter_plans SET calibration_status='pending_calibration' WHERE story_day IS NULL OR TRIM(phase_key)=''"
    )
    works = conn.execute("SELECT id, fact_version FROM works").fetchall()
    for work in works:
        conn.execute(
            """INSERT OR IGNORE INTO fact_versions(id,work_id,version_no,as_of_chapter,source_event_id,reason,created_at)
               VALUES (?,?,0,0,NULL,'V2 migration baseline',?)""",
            (str(uuid4()), work["id"], now_iso()),
        )


def _migrate_model_call_log_history(conn: sqlite3.Connection) -> None:
    """Expose existing generation jobs in the new per-call view once."""
    conn.execute(
        """
        INSERT INTO model_call_logs(
            id, user_id, work_id, generation_job_id, model_profile_id,
            call_kind, provider, model, base_url, status,
            request_json, response_text, response_json, error,
            started_at, first_output_at, completed_at, duration_ms, first_output_ms,
            input_tokens, output_tokens, total_tokens, created_at
        )
        SELECT
            'legacy-job-' || j.id,
            w.user_id,
            j.work_id,
            j.id,
            j.model_profile_id,
            j.kind,
            j.resolved_provider,
            j.resolved_model,
            j.resolved_base_url,
            CASE j.status
                WHEN 'completed' THEN 'success'
                WHEN 'canceled' THEN 'canceled'
                WHEN 'failed' THEN 'failed'
                ELSE j.status
            END,
            j.input_json,
            j.output_json,
            j.output_json,
            j.error,
            COALESCE(j.model_started_at, j.started_at, j.created_at),
            j.model_first_output_at,
            j.completed_at,
            CASE WHEN json_valid(j.metrics_json) THEN json_extract(j.metrics_json, '$.model_ms') END,
            CASE WHEN json_valid(j.metrics_json) THEN json_extract(j.metrics_json, '$.first_output_ms') END,
            j.input_tokens,
            j.output_tokens,
            j.total_tokens,
            j.created_at
        FROM generation_jobs AS j
        JOIN works AS w ON w.id=j.work_id
        WHERE NOT EXISTS (
            SELECT 1 FROM model_call_logs AS existing
            WHERE existing.generation_job_id=j.id
        )
        """
    )


def _migrate_model_call_log_schema(conn: sqlite3.Connection) -> None:
    """Add call-level lifecycle fields without rewriting historical records."""
    for column, definition in {
        "parse_status": "TEXT NOT NULL DEFAULT 'not_recorded'",
        "quality_status": "TEXT NOT NULL DEFAULT 'not_recorded'",
        "adoption_status": "TEXT NOT NULL DEFAULT 'not_recorded'",
        "repair_of_call_id": "TEXT",
    }.items():
        _ensure_column(conn, "model_call_logs", column, definition)
    # Repair metadata was already stored in the request envelope by earlier
    # versions. Promote it when the new explicit column is introduced.
    conn.execute(
        """
        UPDATE model_call_logs
        SET repair_of_call_id=json_extract(request_json, '$.observability.repair_of_call_id')
        WHERE (repair_of_call_id IS NULL OR repair_of_call_id='')
          AND json_valid(request_json)
          AND json_extract(request_json, '$.observability.repair_of_call_id') IS NOT NULL
        """
    )


def _migrate_long_form_structure_schema(conn: sqlite3.Connection) -> None:
    """Add separate long-form pacing coordinates without overloading story time."""
    _ensure_column(conn, "works", "target_chapter_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "works", "average_chapter_words", "INTEGER NOT NULL DEFAULT 2500")
    _ensure_column(conn, "chapter_plans", "volume_id", "TEXT")
    _ensure_column(conn, "chapter_plans", "narrative_stage_id", "TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS story_volumes (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            title TEXT NOT NULL,
            start_chapter INTEGER NOT NULL,
            end_chapter INTEGER NOT NULL,
            target_words INTEGER NOT NULL DEFAULT 0,
            synopsis TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            opposition TEXT NOT NULL DEFAULT '',
            ending_state_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'planned',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(work_id, sequence),
            UNIQUE(work_id, title),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS narrative_stages (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            volume_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            title TEXT NOT NULL,
            start_chapter INTEGER NOT NULL,
            end_chapter INTEGER NOT NULL,
            purpose TEXT NOT NULL DEFAULT '',
            entry_state_json TEXT NOT NULL DEFAULT '{}',
            exit_state_json TEXT NOT NULL DEFAULT '{}',
            allowed_payoffs_json TEXT NOT NULL DEFAULT '[]',
            forbidden_payoffs_json TEXT NOT NULL DEFAULT '[]',
            prerequisites_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'planned',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(volume_id, sequence),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (volume_id) REFERENCES story_volumes(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_story_volumes_range ON story_volumes(work_id, start_chapter, end_chapter);
        CREATE INDEX IF NOT EXISTS idx_narrative_stages_range ON narrative_stages(work_id, start_chapter, end_chapter);
        """
    )
    # Existing works receive a calculated total target.  Their editable
    # volume/stage rows are materialized lazily by repository/bootstrap code.
    conn.execute(
        """UPDATE works
           SET average_chapter_words=2500
           WHERE average_chapter_words IS NULL OR average_chapter_words < 800"""
    )
    conn.execute(
        """UPDATE works
           SET target_chapter_count=MIN(10000, MAX(1, (estimated_words + average_chapter_words - 1) / average_chapter_words))
           WHERE target_chapter_count IS NULL OR target_chapter_count < 1"""
    )


def _migrate_inspiration_schema(conn: sqlite3.Connection) -> None:
    """Persist abstract source models and the original-work blueprint they feed.

    Raw ranking entries remain separate from the derivative editorial models.  This
    lets a work retain a compact, auditable creative brief without making later
    planning prompts depend on source titles, names, or other surface details.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_work_models (
            id TEXT PRIMARY KEY,
            analysis_id TEXT NOT NULL,
            trend_item_id TEXT NOT NULL,
            model_json TEXT NOT NULL DEFAULT '{}',
            completeness TEXT NOT NULL DEFAULT 'low',
            created_at TEXT NOT NULL,
            UNIQUE(analysis_id, trend_item_id),
            FOREIGN KEY (analysis_id) REFERENCES trend_analyses(id) ON DELETE CASCADE,
            FOREIGN KEY (trend_item_id) REFERENCES trend_items(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_source_work_models_analysis
            ON source_work_models(analysis_id, trend_item_id);
        CREATE TABLE IF NOT EXISTS inspiration_blueprints (
            id TEXT PRIMARY KEY,
            analysis_id TEXT NOT NULL,
            idea_index INTEGER NOT NULL,
            content_json TEXT NOT NULL DEFAULT '{}',
            originality_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(analysis_id, idea_index),
            FOREIGN KEY (analysis_id) REFERENCES trend_analyses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS work_inspiration_blueprints (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL UNIQUE,
            blueprint_id TEXT NOT NULL,
            idempotency_key TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(idempotency_key),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (blueprint_id) REFERENCES inspiration_blueprints(id) ON DELETE RESTRICT
        );
        """
    )


def init_db() -> None:
    with transaction() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS works (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                genre TEXT NOT NULL DEFAULT '',
                target_audience TEXT NOT NULL DEFAULT '',
                estimated_words INTEGER NOT NULL DEFAULT 0,
                writing_style TEXT NOT NULL DEFAULT '',
                premise TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS model_profiles (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'openai_compatible',
                base_url TEXT NOT NULL,
                model TEXT NOT NULL,
                encrypted_api_key TEXT NOT NULL DEFAULT '',
                reasoning_effort TEXT NOT NULL DEFAULT 'auto',
                timeout_seconds REAL NOT NULL DEFAULT 90,
                is_default INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_test_status TEXT NOT NULL DEFAULT 'untested',
                last_test_error TEXT NOT NULL DEFAULT '',
                last_test_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, name),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_model_profiles_default ON model_profiles(user_id, is_default);
            CREATE TABLE IF NOT EXISTS proxy_settings (
                user_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                host TEXT NOT NULL DEFAULT '127.0.0.1',
                port INTEGER NOT NULL DEFAULT 10808,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS prompt_settings (
                user_id TEXT NOT NULL,
                prompt_key TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, prompt_key),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS story_bibles (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL DEFAULT '',
                theme TEXT NOT NULL DEFAULT '',
                world TEXT NOT NULL DEFAULT '',
                ending TEXT NOT NULL DEFAULT '',
                style_rules TEXT NOT NULL DEFAULT '',
                locked INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS characters (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                goal TEXT NOT NULL DEFAULT '',
                conflict TEXT NOT NULL DEFAULT '',
                personality TEXT NOT NULL DEFAULT '',
                background TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                knowledge TEXT NOT NULL DEFAULT '',
                story_function TEXT NOT NULL DEFAULT '',
                appearance TEXT NOT NULL DEFAULT '',
                portrayal TEXT NOT NULL DEFAULT '',
                facets_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS plot_arcs (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                title TEXT NOT NULL,
                synopsis TEXT NOT NULL DEFAULT '',
                sequence INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'planned',
                created_at TEXT NOT NULL,
                UNIQUE(work_id, title),
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS chapter_plans (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                chapter_no INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                goal TEXT NOT NULL DEFAULT '',
                conflict TEXT NOT NULL DEFAULT '',
                beats TEXT NOT NULL DEFAULT '[]',
                hook TEXT NOT NULL DEFAULT '',
                pov_character TEXT NOT NULL DEFAULT '',
                opening_state_json TEXT NOT NULL DEFAULT '{}',
                causal_beats_json TEXT NOT NULL DEFAULT '[]',
                knowledge_changes_json TEXT NOT NULL DEFAULT '[]',
                state_changes_json TEXT NOT NULL DEFAULT '[]',
                foreshadow_actions_json TEXT NOT NULL DEFAULT '[]',
                forbidden_reveals_json TEXT NOT NULL DEFAULT '[]',
                ending_state_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'planned',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(work_id, chapter_no),
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS chapters (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                chapter_no INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(work_id, chapter_no),
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS timeline_events (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                event_order INTEGER NOT NULL DEFAULT 0,
                chapter_no INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS foreshadows (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                clue TEXT NOT NULL,
                planted_chapter INTEGER NOT NULL DEFAULT 0,
                expected_reveal_chapter INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                kind TEXT NOT NULL DEFAULT 'clue',
                actual_reveal_chapter INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS character_states (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                character_id TEXT NOT NULL,
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(work_id, character_id),
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
                FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS foreshadow_candidates (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                extraction_id TEXT NOT NULL,
                clue TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'clue',
                planted_chapter INTEGER NOT NULL DEFAULT 0,
                expected_reveal_chapter INTEGER NOT NULL DEFAULT 0,
                evidence TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
                FOREIGN KEY (extraction_id) REFERENCES state_extractions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS generation_runs (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                input_json TEXT NOT NULL DEFAULT '{}',
                output_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'completed',
                created_at TEXT NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS generation_jobs (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                input_json TEXT NOT NULL DEFAULT '{}',
                output_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL DEFAULT 'queued',
                stage_label TEXT NOT NULL DEFAULT '排队中',
                message TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL DEFAULT '',
                model_profile_id TEXT,
                cancel_requested_at TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                resolved_provider TEXT NOT NULL DEFAULT '',
                resolved_model TEXT NOT NULL DEFAULT '',
                resolved_base_url TEXT NOT NULL DEFAULT '',
                model_started_at TEXT,
                model_first_output_at TEXT,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
                UNIQUE(work_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_generation_jobs_status_created
                ON generation_jobs(status, created_at);
            CREATE TABLE IF NOT EXISTS model_call_logs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                work_id TEXT,
                generation_job_id TEXT,
                model_profile_id TEXT,
                call_kind TEXT NOT NULL DEFAULT 'model',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                parse_status TEXT NOT NULL DEFAULT 'not_recorded',
                quality_status TEXT NOT NULL DEFAULT 'not_recorded',
                adoption_status TEXT NOT NULL DEFAULT 'not_recorded',
                repair_of_call_id TEXT,
                request_json TEXT NOT NULL DEFAULT '{}',
                response_text TEXT NOT NULL DEFAULT '',
                response_json TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                first_output_at TEXT,
                completed_at TEXT,
                duration_ms INTEGER,
                first_output_ms INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE SET NULL,
                FOREIGN KEY (generation_job_id) REFERENCES generation_jobs(id) ON DELETE SET NULL,
                FOREIGN KEY (model_profile_id) REFERENCES model_profiles(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_model_call_logs_created
                ON model_call_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_model_call_logs_status_created
                ON model_call_logs(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_model_call_logs_model_created
                ON model_call_logs(model, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_model_call_logs_job
                ON model_call_logs(generation_job_id);
            CREATE TABLE IF NOT EXISTS trend_snapshots (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                captured_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trend_items (
                id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                rank INTEGER NOT NULL DEFAULT 0,
                board TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                synopsis TEXT NOT NULL DEFAULT '',
                metric_label TEXT NOT NULL DEFAULT '',
                metric_value TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                UNIQUE(snapshot_id, source_id),
                FOREIGN KEY (snapshot_id) REFERENCES trend_snapshots(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_trend_items_search ON trend_items(source, category, rank);
            CREATE TABLE IF NOT EXISTS trend_analyses (
                id TEXT PRIMARY KEY,
                query_json TEXT NOT NULL DEFAULT '{}',
                source_item_ids_json TEXT NOT NULL DEFAULT '[]',
                result_json TEXT NOT NULL DEFAULT '{}',
                model_profile_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_inspirations (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                analysis_id TEXT,
                source TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
                FOREIGN KEY (analysis_id) REFERENCES trend_analyses(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS quality_reports (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                chapter_no INTEGER NOT NULL,
                issues_json TEXT NOT NULL DEFAULT '[]',
                score INTEGER NOT NULL DEFAULT 100,
                created_at TEXT NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
            );
            """
        )
        _migrate_state_extraction_schema(conn)
        _migrate_story_planning_schema(conn)
        _cleanup_legacy_character_drafts(conn)
        _migrate_state_engine_schema(conn)
        _migrate_long_form_structure_schema(conn)
        _migrate_inspiration_schema(conn)
        _migrate_model_call_log_schema(conn)
        _migrate_model_call_log_history(conn)
        row = conn.execute("SELECT id FROM users WHERE username = ?", ("demo",)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users(id, username, created_at) VALUES (?, ?, ?)",
                ("demo-user", "demo", now_iso()),
            )
