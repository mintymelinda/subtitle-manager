import sqlite3
from pathlib import Path
from app.config import settings


def connect():
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def initialize():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS media_files (
            id INTEGER PRIMARY KEY,
            relative_path TEXT NOT NULL UNIQUE,
            size INTEGER,
            modified_at REAL,
            status TEXT NOT NULL DEFAULT 'scanned',
            last_scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS patch_history (
            id INTEGER PRIMARY KEY,
            relative_path TEXT NOT NULL,
            subtitle_name TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)


def record_scan(relative_path: str, size: int | None, modified_at: float | None):
    with connect() as db:
        db.execute("""
            INSERT INTO media_files(relative_path, size, modified_at)
            VALUES (?, ?, ?)
            ON CONFLICT(relative_path) DO UPDATE SET
                size=excluded.size,
                modified_at=excluded.modified_at,
                last_scanned_at=CURRENT_TIMESTAMP
        """, (relative_path, size, modified_at))
