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
            last_scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            has_verified_english_subtitle INTEGER NOT NULL DEFAULT 0,
            inspection_json TEXT NOT NULL DEFAULT '{}'
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
            review_decision TEXT NOT NULL DEFAULT 'pending',
            review_updated_at TEXT,
            provider_download_remaining INTEGER,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(media_relative_path, file_id)
        );
        """)
        media_columns = {row[1] for row in db.execute("PRAGMA table_info(media_files)")}
        if "has_verified_english_subtitle" not in media_columns:
            db.execute("ALTER TABLE media_files ADD COLUMN has_verified_english_subtitle INTEGER NOT NULL DEFAULT 0")
        if "inspection_json" not in media_columns:
            db.execute("ALTER TABLE media_files ADD COLUMN inspection_json TEXT NOT NULL DEFAULT '{}'")
        candidate_columns = {row[1] for row in db.execute("PRAGMA table_info(subtitle_candidates)")}
        if "review_decision" not in candidate_columns:
            db.execute("ALTER TABLE subtitle_candidates ADD COLUMN review_decision TEXT NOT NULL DEFAULT 'pending'")
        if "review_updated_at" not in candidate_columns:
            db.execute("ALTER TABLE subtitle_candidates ADD COLUMN review_updated_at TEXT")
        db.executescript("""
        CREATE INDEX IF NOT EXISTS idx_subtitle_candidates_media
            ON subtitle_candidates(media_relative_path);
        CREATE INDEX IF NOT EXISTS idx_subtitle_candidates_status
            ON subtitle_candidates(status);
        """)


def record_scan(relative_path: str, size: int | None, modified_at: float | None,
                inspection: dict | None = None):
    with connect() as db:
        db.execute("""
            INSERT INTO media_files(
                relative_path, size, modified_at, has_verified_english_subtitle, inspection_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(relative_path) DO UPDATE SET
                size=excluded.size,
                modified_at=excluded.modified_at,
                has_verified_english_subtitle=excluded.has_verified_english_subtitle,
                inspection_json=excluded.inspection_json,
                last_scanned_at=CURRENT_TIMESTAMP
        """, (
            relative_path, size, modified_at,
            int(bool((inspection or {}).get("has_verified_english_subtitle"))),
            json.dumps(inspection or {}),
        ))


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
                review_decision=CASE
                    WHEN subtitle_candidates.media_size != excluded.media_size
                      OR subtitle_candidates.media_modified_at != excluded.media_modified_at
                    THEN 'pending' ELSE subtitle_candidates.review_decision END,
                review_updated_at=CASE
                    WHEN subtitle_candidates.media_size != excluded.media_size
                      OR subtitle_candidates.media_modified_at != excluded.media_modified_at
                    THEN NULL ELSE subtitle_candidates.review_updated_at END,
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



def list_candidates(media_relative_path: str | None = None) -> list[dict]:
    query = """
        SELECT c.*, m.size AS current_media_size, m.modified_at AS current_media_modified_at,
               m.has_verified_english_subtitle, m.inspection_json
        FROM subtitle_candidates c
        LEFT JOIN media_files m ON m.relative_path = c.media_relative_path
    """
    params: tuple = ()
    if media_relative_path is not None:
        query += " WHERE c.media_relative_path=?"
        params = (media_relative_path,)
    query += " ORDER BY c.media_relative_path, c.match_score DESC, c.id"
    with connect() as db:
        rows = db.execute(query, params).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["match_reasons"] = json.loads(item.get("match_reasons") or "[]")
        item["inspection"] = json.loads(item.pop("inspection_json", "{}") or "{}")
        item["is_stale"] = (
            item.get("current_media_size") != item.get("media_size")
            or item.get("current_media_modified_at") != item.get("media_modified_at")
        )
        results.append(item)
    return results


def set_candidate_decision(candidate_id: int, decision: str) -> dict | None:
    if decision not in {"approved", "rejected"}:
        raise ValueError("Decision must be approved or rejected.")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        candidate = db.execute(
            "SELECT id, media_relative_path, status, review_decision FROM subtitle_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        if candidate is None:
            return None
        if candidate["status"] != "staged":
            raise ValueError("Only successfully staged candidates can be reviewed.")
        if decision == "approved":
            other = db.execute(
                "SELECT id FROM subtitle_candidates WHERE media_relative_path=? AND review_decision='approved' AND id!=?",
                (candidate["media_relative_path"], candidate_id),
            ).fetchone()
            if other:
                raise ValueError("Another candidate is already approved for this media file. Reject it first.")
        db.execute(
            "UPDATE subtitle_candidates SET review_decision=?, review_updated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (decision, candidate_id),
        )
        updated = db.execute(
            "SELECT id, media_relative_path, review_decision, review_updated_at FROM subtitle_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        return dict(updated)
