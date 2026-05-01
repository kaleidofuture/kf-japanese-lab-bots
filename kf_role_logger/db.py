"""SQLite layer for KF RoleLogger.

Five tables (see 08_role_logger_design.md §4.1):
- members        : per Discord user_id, identity row (first_seen, total_sessions)
- sessions       : one row per join-leave cycle (AUTOINCREMENT id, left_at NULL = active)
- role_events    : every role grant/revoke, with role_name snapshot + source tag
- promotions     : history of Newcomer->Member auto-promotions, joined to session_id
- bot_state      : key/value flags (e.g. backfill_done_at)

Pure functions over a sqlite3 connection. The main module owns the connection
and passes it in; this keeps unit-testing trivial (just pass `:memory:`).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    user_id           INTEGER PRIMARY KEY,
    first_seen_at     TEXT NOT NULL,
    total_sessions    INTEGER NOT NULL DEFAULT 1,
    last_seen_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    joined_at       TEXT NOT NULL,
    left_at         TEXT,
    FOREIGN KEY (user_id) REFERENCES members(user_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user   ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(left_at);

CREATE TABLE IF NOT EXISTS role_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    event_type     TEXT NOT NULL,
    role_id        INTEGER NOT NULL,
    role_name      TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    source         TEXT,
    FOREIGN KEY (user_id) REFERENCES members(user_id)
);
CREATE INDEX IF NOT EXISTS idx_events_user_time ON role_events(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_role      ON role_events(role_id, event_type);

CREATE TABLE IF NOT EXISTS promotions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL,
    session_id            INTEGER NOT NULL,
    promoted_at           TEXT NOT NULL,
    newcomer_granted_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES members(user_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_promotions_user ON promotions(user_id);

CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_utc() -> str:
    """ISO8601 UTC timestamp, second precision, with explicit `+00:00`."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_iso(dt: datetime | None) -> str | None:
    """Coerce a datetime (naive or aware) to ISO8601 UTC, or return None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat()


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a sqlite3 connection with sane defaults and FK enforcement."""
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ---- members ----------------------------------------------------------------


def member_exists(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM members WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row is not None


def insert_member(
    conn: sqlite3.Connection,
    user_id: int,
    first_seen_at: str,
    last_seen_at: str,
) -> None:
    """INSERT a new member row. No-op if user_id already exists."""
    conn.execute(
        """
        INSERT OR IGNORE INTO members (user_id, first_seen_at, total_sessions, last_seen_at)
        VALUES (?, ?, 1, ?)
        """,
        (user_id, first_seen_at, last_seen_at),
    )


def increment_session_count(conn: sqlite3.Connection, user_id: int) -> None:
    """Bump total_sessions for a re-joining member."""
    conn.execute(
        "UPDATE members SET total_sessions = total_sessions + 1 WHERE user_id = ?",
        (user_id,),
    )


def update_last_seen(conn: sqlite3.Connection, user_id: int, ts: str) -> None:
    conn.execute(
        "UPDATE members SET last_seen_at = ? WHERE user_id = ?",
        (ts, user_id),
    )


# ---- sessions ---------------------------------------------------------------


def insert_session(
    conn: sqlite3.Connection,
    user_id: int,
    joined_at: str,
) -> int:
    """Insert an active (left_at=NULL) session row, return its id."""
    cur = conn.execute(
        "INSERT INTO sessions (user_id, joined_at, left_at) VALUES (?, ?, NULL)",
        (user_id, joined_at),
    )
    return int(cur.lastrowid)


def close_active_session(
    conn: sqlite3.Connection,
    user_id: int,
    left_at: str,
) -> None:
    """Set left_at on the active (NULL) session for this user, if any."""
    conn.execute(
        """
        UPDATE sessions
        SET left_at = ?
        WHERE user_id = ? AND left_at IS NULL
        """,
        (left_at, user_id),
    )


def get_active_session_id(conn: sqlite3.Connection, user_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM sessions WHERE user_id = ? AND left_at IS NULL",
        (user_id,),
    ).fetchone()
    return int(row["id"]) if row else None


# ---- role_events ------------------------------------------------------------


def insert_role_event(
    conn: sqlite3.Connection,
    user_id: int,
    event_type: str,
    role_id: int,
    role_name: str,
    timestamp: str,
    source: str,
) -> None:
    """Append a role grant/revoke event."""
    conn.execute(
        """
        INSERT INTO role_events
            (user_id, event_type, role_id, role_name, timestamp, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, event_type, role_id, role_name, timestamp, source),
    )


# ---- promotions -------------------------------------------------------------


def find_promotion_candidates(
    conn: sqlite3.Connection,
    newcomer_role_id: int,
    grace_days: float,
) -> list[tuple[int, int, str]]:
    """Return (user_id, session_id, newcomer_granted_at) tuples ready for auto-promotion.

    A candidate is a member whose currently-active session contains a Newcomer
    `added` event older than `grace_days`, and whose `promotions` table has no
    row for that session_id (idempotency per session).
    """
    rows = conn.execute(
        """
        WITH active_sessions AS (
            SELECT id AS session_id, user_id, joined_at
            FROM sessions
            WHERE left_at IS NULL
        ),
        latest_newcomer_in_session AS (
            SELECT s.session_id, s.user_id,
                   MAX(re.timestamp) AS granted_at
            FROM active_sessions s
            JOIN role_events re
                ON re.user_id    = s.user_id
               AND re.role_id    = ?
               AND re.event_type = 'added'
               AND re.timestamp >= s.joined_at
            GROUP BY s.session_id, s.user_id
        )
        SELECT l.user_id, l.session_id, l.granted_at
        FROM latest_newcomer_in_session l
        LEFT JOIN promotions p ON p.session_id = l.session_id
        WHERE p.id IS NULL
          AND (julianday('now') - julianday(l.granted_at)) >= ?
        """,
        (newcomer_role_id, grace_days),
    ).fetchall()
    return [(int(r["user_id"]), int(r["session_id"]), str(r["granted_at"])) for r in rows]


def record_promotion(
    conn: sqlite3.Connection,
    user_id: int,
    session_id: int,
    newcomer_granted_at: str,
    promoted_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO promotions
            (user_id, session_id, promoted_at, newcomer_granted_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, session_id, promoted_at, newcomer_granted_at),
    )


# ---- bot_state --------------------------------------------------------------


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM bot_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO bot_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def backfill_already_done(conn: sqlite3.Connection) -> bool:
    return get_state(conn, "backfill_done_at") is not None


def mark_backfill_done(conn: sqlite3.Connection) -> None:
    set_state(conn, "backfill_done_at", now_utc())
