import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import settings


SQLITE_BUSY_TIMEOUT_MS = 30_000


@contextmanager
def connect():
    """Open a short-lived SQLite connection with bounded lock waiting.

    The context manager commits successful work, rolls back failures, and always
    closes the connection. sqlite3.Connection's built-in context manager does
    not close connections, which can otherwise leave file handles alive longer
    than intended.
    """
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        db_path,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        check_same_thread=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize():
    with connect() as db:
        # WAL allows dashboard readers to continue while background workers
        # commit scan/download/job progress. busy_timeout handles brief writer
        # contention without immediately surfacing SQLITE_BUSY to API callers.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
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
            patch_status TEXT NOT NULL DEFAULT 'not_started',
            patch_started_at TEXT,
            patch_completed_at TEXT,
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
        if "patch_status" not in candidate_columns:
            db.execute("ALTER TABLE subtitle_candidates ADD COLUMN patch_status TEXT NOT NULL DEFAULT 'not_started'")
        if "patch_started_at" not in candidate_columns:
            db.execute("ALTER TABLE subtitle_candidates ADD COLUMN patch_started_at TEXT")
        if "patch_completed_at" not in candidate_columns:
            db.execute("ALTER TABLE subtitle_candidates ADD COLUMN patch_completed_at TEXT")
        history_columns = {row[1] for row in db.execute("PRAGMA table_info(patch_history)")}
        if "candidate_id" not in history_columns:
            db.execute("ALTER TABLE patch_history ADD COLUMN candidate_id INTEGER")
        if "backup_path" not in history_columns:
            db.execute("ALTER TABLE patch_history ADD COLUMN backup_path TEXT")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            phase TEXT,
            current_file TEXT,
            total INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            succeeded INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS job_items (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            media_path TEXT NOT NULL,
            candidate_id INTEGER,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            review_decision TEXT,
            patch_status TEXT,
            error TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_job_items_job ON job_items(job_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_job_items_job_media ON job_items(job_id, media_path);
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
                patch_status=CASE
                    WHEN (subtitle_candidates.media_size != excluded.media_size
                      OR subtitle_candidates.media_modified_at != excluded.media_modified_at)
                      AND subtitle_candidates.patch_status='patched'
                    THEN 'not_started' ELSE subtitle_candidates.patch_status END,
                patch_started_at=CASE
                    WHEN (subtitle_candidates.media_size != excluded.media_size
                      OR subtitle_candidates.media_modified_at != excluded.media_modified_at)
                      AND subtitle_candidates.patch_status='patched'
                    THEN NULL ELSE subtitle_candidates.patch_started_at END,
                patch_completed_at=CASE
                    WHEN (subtitle_candidates.media_size != excluded.media_size
                      OR subtitle_candidates.media_modified_at != excluded.media_modified_at)
                      AND subtitle_candidates.patch_status='patched'
                    THEN NULL ELSE subtitle_candidates.patch_completed_at END,
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



def clear_pending_review_queue() -> list[str]:
    """Remove candidates from prior scans, retaining only active/recovery operations.

    Running/interrupted injections remain available for safety and recovery.
    Return staged paths so callers can safely clean up orphaned review downloads.
    """
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            """SELECT staged_path FROM subtitle_candidates
               WHERE patch_status NOT IN ('running', 'interrupted')"""
        ).fetchall()
        db.execute(
            """DELETE FROM subtitle_candidates
               WHERE patch_status NOT IN ('running', 'interrupted')"""
        )
        return [row["staged_path"] for row in rows if row["staged_path"]]


def clear_saved_data() -> list[str]:
    """Delete saved jobs, scan results, candidates, and patch history.

    Refuse to clear while any background job is active. Return staged files for
    safe filesystem cleanup by the API layer.
    """
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT id FROM jobs WHERE status IN ('queued', 'running') LIMIT 1"
        ).fetchone()
        if active:
            raise ValueError("Wait for active jobs to finish before clearing saved data.")
        injecting = db.execute(
            "SELECT id FROM subtitle_candidates WHERE patch_status='running' LIMIT 1"
        ).fetchone()
        if injecting:
            raise ValueError("Wait for active injections to finish before clearing saved data.")
        staged = db.execute(
            "SELECT staged_path FROM subtitle_candidates WHERE staged_path IS NOT NULL"
        ).fetchall()
        db.execute("DELETE FROM job_items")
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM subtitle_candidates")
        db.execute("DELETE FROM media_files")
        db.execute("DELETE FROM patch_history")
        return [row["staged_path"] for row in staged if row["staged_path"]]


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
    if decision not in {"pending", "approved", "rejected"}:
        raise ValueError("Decision must be pending, approved, or rejected.")
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



def list_media_files() -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT relative_path, size, modified_at, has_verified_english_subtitle, inspection_json, last_scanned_at FROM media_files ORDER BY relative_path"
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["has_verified_english_subtitle"] = bool(item["has_verified_english_subtitle"])
        item["inspection"] = json.loads(item.pop("inspection_json") or "{}")
        results.append(item)
    return results


def get_candidate(candidate_id: int) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM subtitle_candidates WHERE id=?", (candidate_id,)).fetchone()
    return dict(row) if row else None


def claim_candidate_patch(candidate_id: int, *, test_mode: bool = False) -> dict | None:
    """Atomically reserve one approved candidate for a single patch operation."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM subtitle_candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != "staged" or row["review_decision"] != "approved":
            raise ValueError("This exact candidate must be staged and explicitly approved before injection.")
        if row["patch_status"] == "patched":
            raise ValueError("This candidate has already been injected.")
        if row["patch_status"] == "running":
            raise ValueError("An injection for this candidate is already running.")
        if row["patch_status"] == "interrupted":
            raise ValueError("A previous injection was interrupted. Inspect the media and recovery copy before retrying.")
        if test_mode:
            other_running = db.execute(
                "SELECT id FROM subtitle_candidates WHERE patch_status='running' AND id!=? LIMIT 1",
                (candidate_id,),
            ).fetchone()
            if other_running:
                raise ValueError("Test mode permits only one injection to run at a time.")
        db.execute(
            """UPDATE subtitle_candidates
               SET patch_status='running', patch_started_at=CURRENT_TIMESTAMP,
                   patch_completed_at=NULL, updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND patch_status IN ('not_started', 'failed')""",
            (candidate_id,),
        )
        claimed = db.execute("SELECT * FROM subtitle_candidates WHERE id=?", (candidate_id,)).fetchone()
        return dict(claimed)


def finish_candidate_patch(candidate_id: int, status: str, details: dict,
                           backup_path: str | None = None) -> None:
    if status not in {"patched", "failed"}:
        raise ValueError("Invalid patch result status.")
    with connect() as db:
        db.execute(
            """UPDATE subtitle_candidates
               SET patch_status=?, patch_completed_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, candidate_id),
        )
        row = db.execute(
            "SELECT media_relative_path, file_name FROM subtitle_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        if row:
            db.execute(
                """INSERT INTO patch_history(
                       relative_path, subtitle_name, status, details, candidate_id, backup_path
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (row["media_relative_path"], row["file_name"], status,
                 json.dumps(details), candidate_id, backup_path),
            )


def recover_interrupted_patches() -> int:
    """Mark operations left running by a process restart; never guess whether to retry."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT id, media_relative_path, file_name FROM subtitle_candidates WHERE patch_status='running'"
        ).fetchall()
        for row in rows:
            details = {
                "error": "Application restarted while injection was running. Inspect the media file and retained recovery copies before taking further action.",
                "recovery_required": True,
            }
            db.execute(
                """UPDATE subtitle_candidates
                   SET patch_status='interrupted', patch_completed_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (row["id"],),
            )
            db.execute(
                """INSERT INTO patch_history(
                       relative_path, subtitle_name, status, details, candidate_id
                   ) VALUES (?, ?, 'interrupted', ?, ?)""",
                (row["media_relative_path"], row["file_name"],
                 json.dumps(details), row["id"]),
            )
        return len(rows)


def create_job(kind: str, total: int = 0, metadata: dict | None = None) -> dict:
    if kind not in {"scan", "inject", "review"}:
        raise ValueError("Unsupported job type.")
    with connect() as db:
        cursor = db.execute(
            """INSERT INTO jobs(kind, status, total, metadata_json)
               VALUES (?, 'queued', ?, ?)""",
            (kind, max(0, int(total)), json.dumps(metadata or {})),
        )
        row = db.execute("SELECT * FROM jobs WHERE id=?", (cursor.lastrowid,)).fetchone()
        return _job_row(row)


def _job_row(row):
    if row is None:
        return None
    item = dict(row)
    item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
    return item


def start_job(job_id: int):
    with connect() as db:
        db.execute(
            """UPDATE jobs SET status='running', started_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'""",
            (job_id,),
        )


def update_job_progress(job_id: int, *, phase: str | None = None,
                        current_file: str | None = None, completed: int | None = None,
                        succeeded: int | None = None, failed: int | None = None,
                        total: int | None = None):
    assignments = ["updated_at=CURRENT_TIMESTAMP"]
    values = []
    for column, value in (("phase", phase), ("current_file", current_file),
                          ("completed", completed), ("succeeded", succeeded),
                          ("failed", failed), ("total", total)):
        if value is not None:
            assignments.append(f"{column}=?")
            values.append(value)
    values.append(job_id)
    with connect() as db:
        db.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id=?", values)


def record_job_item(job_id: int, *, media_path: str, status: str,
                    phase: str, candidate_id: int | None = None,
                    review_decision: str | None = None,
                    patch_status: str | None = None, error: str | None = None,
                    details: dict | None = None):
    with connect() as db:
        db.execute(
            """INSERT INTO job_items(job_id, media_path, candidate_id, status, phase,
                   review_decision, patch_status, error, details_json, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ?='running' THEN NULL ELSE CURRENT_TIMESTAMP END)
               ON CONFLICT(job_id, media_path) DO UPDATE SET
                   candidate_id=COALESCE(excluded.candidate_id, job_items.candidate_id),
                   status=excluded.status, phase=excluded.phase,
                   review_decision=COALESCE(excluded.review_decision, job_items.review_decision),
                   patch_status=COALESCE(excluded.patch_status, job_items.patch_status),
                   error=excluded.error, details_json=excluded.details_json,
                   completed_at=excluded.completed_at""",
            (job_id, media_path, candidate_id, status, phase,
             review_decision, patch_status, (error or "")[:500],
             json.dumps(details or {}), status),
        )


def finish_job(job_id: int, status: str, error: str | None = None):
    if status not in {"completed", "failed", "cancelled", "interrupted"}:
        raise ValueError("Invalid terminal job state.")
    with connect() as db:
        db.execute(
            """UPDATE jobs SET status=?, error=?, finished_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('queued','running')""",
            (status, (error or "")[:500] or None, job_id),
        )


def get_job(job_id: int) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = _job_row(row)
        result["items"] = [
            dict(item) for item in db.execute(
                "SELECT * FROM job_items WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
        ]
        for item in result["items"]:
            item["details"] = json.loads(item.pop("details_json", "{}") or "{}")
        return result


def list_jobs(limit: int = 25) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (max(1, min(100, limit)),)
        ).fetchall()
        results = []
        for row in rows:
            item = _job_row(row)
            item["items"] = db.execute(
                "SELECT COUNT(*) FROM job_items WHERE job_id=?", (item["id"],)
            ).fetchone()[0]
            results.append(item)
        return results


def recover_interrupted_jobs() -> int:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT id, kind FROM jobs WHERE status IN ('queued','running')"
        ).fetchall()
        for row in rows:
            db.execute("UPDATE job_items SET status='interrupted', phase='recovery', error='Application restarted before this file finished.', completed_at=CURRENT_TIMESTAMP WHERE job_id=? AND status='running'", (row["id"],))
            db.execute(
                """UPDATE jobs SET status='interrupted',
                   error='Application restarted before this job reached a terminal state.',
                   finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (row["id"],),
            )
        return len(rows)
