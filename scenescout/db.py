"""SQLite spine for the pipeline. One DB, one table per stage output."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "scenescout.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    tier          INTEGER,
    home_url      TEXT,
    sitemap_url   TEXT,
    events_url    TEXT,
    extra_urls    TEXT,
    platform      TEXT,
    extract_route TEXT,
    route_detail  TEXT,
    status        TEXT,
    http_status   INTEGER,
    last_checked  TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS fetch_cache (
    url        TEXT PRIMARY KEY,
    fetched_at TEXT,
    status     INTEGER,
    content    BLOB
);

CREATE TABLE IF NOT EXISTS raw_events (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER REFERENCES sources(id),
    channel    TEXT,
    url        TEXT,
    payload    TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_events(source_id);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER REFERENCES sources(id),
    raw_id         INTEGER REFERENCES raw_events(id),
    title          TEXT,
    presenter      TEXT,
    venue_name     TEXT,
    venue_address  TEXT,
    venue_city     TEXT,
    start_date     TEXT,
    start_time     TEXT,
    end_date       TEXT,
    end_time       TEXT,
    occurrences    TEXT,
    category_ids   TEXT,
    url            TEXT,
    ticket_url     TEXT,
    phone          TEXT,
    description    TEXT,
    price_low      INTEGER,
    price_high     INTEGER,
    is_free        INTEGER,
    relevance      TEXT,
    relevance_reason TEXT,
    content_hash   TEXT UNIQUE,
    first_seen     TEXT,
    last_seen      TEXT,
    dedupe_group   INTEGER,
    verdict        TEXT,
    verdict_reason TEXT,
    scene_match    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_date);

-- Counters a later stage needs but cannot recompute: normalize deletes the
-- rows it rejects, so the export's report would otherwise report zero.
CREATE TABLE IF NOT EXISTS run_stats (
    stage      TEXT,
    key        TEXT,
    value      INTEGER,
    updated_at TEXT,
    PRIMARY KEY (stage, key)
);

CREATE TABLE IF NOT EXISTS scene_orgs (
    scene_id INTEGER PRIMARY KEY,
    name     TEXT,
    slug     TEXT,
    url      TEXT
);

CREATE TABLE IF NOT EXISTS scene_listings (
    id             INTEGER PRIMARY KEY,
    scene_event_id INTEGER,
    title          TEXT,
    venue          TEXT,
    start_date     TEXT,
    end_date       TEXT,
    dates          TEXT,
    is_range       INTEGER DEFAULT 0,
    url            TEXT,
    origin         TEXT,
    scraped_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scene_event_id
    ON scene_listings(scene_event_id) WHERE scene_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_scene_xlsx
    ON scene_listings(title, venue, start_date) WHERE scene_event_id IS NULL;
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Columns added after the first schema version, applied to existing databases.
MIGRATIONS = {
    "scene_listings": {"dates": "TEXT", "is_range": "INTEGER DEFAULT 0"},
    "events": {"occurrences": "TEXT"},
}


def connect(path: Path = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    # Several stages probe sources concurrently; WAL plus a busy timeout keeps
    # those writer connections from tripping over each other.
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def save_stats(conn, stage: str, stats: dict) -> None:
    conn.execute("DELETE FROM run_stats WHERE stage = ?", (stage,))
    for key, value in stats.items():
        if isinstance(value, int):
            conn.execute(
                "INSERT INTO run_stats (stage, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (stage, key, value, now()),
            )
    conn.commit()


def load_stats(conn, stage: str) -> dict:
    return {r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM run_stats WHERE stage = ?", (stage,))}
