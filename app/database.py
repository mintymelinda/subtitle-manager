import json
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
        CREATE TABLE IF NOT EXISTS subtitle_candidates (
            id INTEGER PRIMARY KEY,
            media_relative_path TEXT NOT NULL,
            media_size INTEGER,
            media_modified_at REAL,
            subtitle_id TEXT,
            file_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            language TEXT NOT NULL,
            format TEXT NOT NULL,
            content_language TEXT,
            content_language_confidence REAL,
            content_status TEXT NOT NULL DEFAULT 'unknown',
            release TEXT,
            moviehash_match INTEGER NOT NULL DEFAULT 0,
            download_count INTEGER NOT NULL DEFAULT 0,
            rating REAL,
            trusted_uploader INTEGER NOT NULL DEFAULT 0,
            hearing_impaired INTEGER NOT NULL DEFAULT 0,
            ai_translated INTEGER NOT NULL DEFAULT 0,
            match_score INTEGER NOT NULL DEFAULT 0,
            match_reasons TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'discovered',
            staged_path TEXT,
            provider_download_remaining INTEGER,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(media_relative_path, file_id)
        );
        CREATE INDEX IF NOT EXISTS idx_subtitle_candidates_media
            ON subtitle_candidates(media_relative_path);
        CREATE INDEX IF NOT EXISTS idx_subtitle_candidates_status
            ON subtitle_candidates(status);
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


def save_candidate(media_relative_path: str, media_size: int, media_modified_at: float,
                   candidate: dict) -> dict:
    with connect() as db:
        db.execute("""
            INSERT INTO subtitle_candidates(
                media_relative_path, media_size, media_modified_at, subtitle_id,
                file_id, file_name, language, format, release, moviehash_match,
                download_count, rating, trusted_uploader, hearing_impaired,
                ai_translated, match_score, match_reasons
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_relative_path, file_id) DO UPDATE SET
                media_size=excluded.media_size,
                media_modified_at=excluded.media_modified_at,
                subtitle_id=excluded.subtitle_id,
                file_name=excluded.file_name,
                language=excluded.language,
                format=excluded.format,
                release=excluded.release,
                moviehash_match=excluded.moviehash_match,
                download_count=excluded.download_count,
                rating=excluded.rating,
                trusted_uploader=excluded.trusted_uploader,
                hearing_impaired=excluded.hearing_impaired,
                ai_translated=excluded.ai_translated,
                match_score=excluded.match_score,
                match_reasons=excluded.match_reasons,
                status=CASE WHEN subtitle_candidates.status IN ('staged', 'rejected_non_english')
                            THEN subtitle_candidates.status ELSE 'discovered' END,
                updated_at=CURRENT_TIMESTAMP
        """, (
            media_relative_path, media_size, media_modified_at,
            str(candidate.get("subtitle_id") or ""), candidate["file_id"],
            candidate["file_name"], candidate["language"], candidate["format"],
            candidate.get("release", ""), int(candidate.get("moviehash_match", False)),
            candidate.get("download_count", 0), candidate.get("rating"),
            int(candidate.get("trusted_uploader", False)),
            int(candidate.get("hearing_impaired", False)),
            int(candidate.get("ai_translated", False)), candidate.get("match_score", 0),
            json.dumps(candidate.get("match_reasons", [])),
        ))
        row = db.execute("""
            SELECT id, status, staged_path, content_language, content_language_confidence, content_status FROM subtitle_candidates
            WHERE media_relative_path=? AND file_id=?
        """, (media_relative_path, candidate["file_id"])).fetchone()
        return dict(row)


def mark_candidate_staged(candidate_id: int, staged_path: str,
                          remaining: int | None = None, assessment: dict | None = None):
    with connect() as db:
        db.execute("""
            UPDATE subtitle_candidates
            SET status='staged', staged_path=?, provider_download_remaining=?,
                content_language=?, content_language_confidence=?, content_status=?,
                last_error=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (
            staged_path, remaining,
            (assessment or {}).get('content_language'),
            (assessment or {}).get('content_confidence'),
            (assessment or {}).get('content_status', 'unknown'), candidate_id,
        ))


def mark_candidate_error(candidate_id: int, error: str, status: str = 'download_failed'):
    with connect() as db:
        db.execute("""
            UPDATE subtitle_candidates
            SET status=?, last_error=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status NOT IN ('staged', 'rejected_non_english')
        """, (status, error[:500], candidate_id))
