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
        row = conn.execute("SELECT id FROM users WHERE username = ?", ("demo",)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users(id, username, created_at) VALUES (?, ?, ?)",
                ("demo-user", "demo", now_iso()),
            )
