import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.config import DATABASE_URL
from app.utils import now_iso


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
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_state_extraction_schema(conn: sqlite3.Connection) -> None:
    """为已有 MVP 数据库补齐 Phase 1 字段；新库和旧库都可启动。"""
    _ensure_column(conn, "character_states", "as_of_chapter", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "character_states", "source_version_id", "TEXT")
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
            model TEXT NOT NULL DEFAULT 'fallback',
            raw_json TEXT NOT NULL DEFAULT '{}',
            warning TEXT NOT NULL DEFAULT '',
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
            evidence TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
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
                created_at TEXT NOT NULL,
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
        row = conn.execute("SELECT id FROM users WHERE username = ?", ("demo",)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users(id, username, created_at) VALUES (?, ?, ?)",
                ("demo-user", "demo", now_iso()),
            )
