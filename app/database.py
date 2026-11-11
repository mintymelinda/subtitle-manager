import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import settings
from app.observability import timed_stage


SQLITE_BUSY_TIMEOUT_MS = 30_000


@contextmanager
def connect(operation: str | None = None, *, media_path: str | None = None):
    """Open a short-lived SQLite connection with bounded lock waiting.

    The context manager commits successful work, rolls back failures, and always
    closes the connection. sqlite3.Connection's built-in context manager does
    not close connections, which can otherwise leave file handles alive longer
    than intended. An optional operation name enables targeted connection and
    transaction timing for scan profiling.
    """
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if operation:
        with timed_stage(f"{operation}_db_connection_setup", media_path=media_path):
            connection = sqlite3.connect(
                db_path,
                timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
                check_same_thread=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
    else:
        connection = sqlite3.connect(
            db_path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
    try:
        yield connection
        if operation:
            with timed_stage(f"{operation}_db_commit", media_path=media_path):
                connection.commit()
        else:
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        if operation:
            with timed_stage(f"{operation}_db_close", media_path=media_path):
                connection.close()
        else:
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
            modified_ns INTEGER,
            device INTEGER,
            inode INTEGER,
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
            media_modified_ns INTEGER,
            media_device INTEGER,
            media_inode INTEGER,
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
        if "modified_ns" not in media_columns:
            db.execute("ALTER TABLE media_files ADD COLUMN modified_ns INTEGER")
        if "device" not in media_columns:
            db.execute("ALTER TABLE media_files ADD COLUMN device INTEGER")
        if "inode" not in media_columns:
            db.execute("ALTER TABLE media_files ADD COLUMN inode INTEGER")
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
        if "media_modified_ns" not in candidate_columns:
            db.execute("ALTER TABLE subtitle_candidates ADD COLUMN media_modified_ns INTEGER")
        if "media_device" not in candidate_columns:
            db.execute("ALTER TABLE subtitle_candidates ADD COLUMN media_device INTEGER")
        if "media_inode" not in candidate_columns:
            db.execute("ALTER TABLE subtitle_candidates ADD COLUMN media_inode INTEGER")
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
        CREATE TABLE IF NOT EXISTS provider_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT NOT NULL,
            error_code TEXT,
            message TEXT NOT NULL,
            retry_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)


def _scan_write(writer, operation: str, callback, *, media_path: str | None = None):
    """Execute a scan mutation through the dedicated writer when supplied."""
    if writer is not None:
        return writer.submit(operation, callback, media_path=media_path)
    with connect(operation, media_path=media_path) as connection:
        with timed_stage("scan_db_sql", media_path=media_path,
                         database_operation=operation):
            return callback(connection)


def get_cached_scan(relative_path: str, stat_result, *, writer=None) -> dict | None:
    """Return cached inspection only when filesystem identity is unchanged."""
    with connect() as db:
        row = db.execute(
            """SELECT inspection_json FROM media_files
               WHERE relative_path=? AND size=? AND modified_ns=? AND device=? AND inode=?""",
            (relative_path, stat_result.st_size, stat_result.st_mtime_ns,
             stat_result.st_dev, stat_result.st_ino),
        ).fetchone()
    if row is None:
        return None
    try:
        inspection = json.loads(row["inspection_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(inspection, dict) or not inspection:
        return None

    def touch(connection):
        connection.execute(
            """UPDATE media_files SET last_scanned_at=CURRENT_TIMESTAMP
               WHERE relative_path=? AND last_scanned_at < CURRENT_TIMESTAMP""",
            (relative_path,),
        )
    _scan_write(writer, "cache_touch", touch, media_path=relative_path)
    return inspection

def record_scan(relative_path: str, size: int | None, modified_at: float | None,
                inspection: dict | None = None, *, modified_ns: int | None = None,
                device: int | None = None, inode: int | None = None, writer=None):
    def persist(db):
        with timed_stage("scan_record_db_previous_lookup", media_path=relative_path):
            previous = db.execute(
                "SELECT modified_ns, device, inode FROM media_files WHERE relative_path=?",
                (relative_path,),
            ).fetchone()
        if previous is not None and (
            previous["modified_ns"], previous["device"], previous["inode"]
        ) != (modified_ns, device, inode):
            with timed_stage("scan_record_db_invalidate_candidates", media_path=relative_path):
                db.execute(
                    """UPDATE subtitle_candidates
                       SET review_decision='pending', review_updated_at=NULL,
                           patch_status=CASE WHEN patch_status='patched' THEN 'not_started' ELSE patch_status END,
                           patch_started_at=CASE WHEN patch_status='patched' THEN NULL ELSE patch_started_at END,
                           patch_completed_at=CASE WHEN patch_status='patched' THEN NULL ELSE patch_completed_at END,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE media_relative_path=? AND patch_status!='running'""",
                    (relative_path,),
                )
        inspection_json = json.dumps(inspection or {})
        verified = int(bool((inspection or {}).get("has_verified_english_subtitle")))
        with timed_stage("scan_record_db_media_upsert", media_path=relative_path):
            db.execute(
                """INSERT INTO media_files(
                    relative_path, size, modified_at, modified_ns, device, inode,
                    has_verified_english_subtitle, inspection_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    size=excluded.size, modified_at=excluded.modified_at,
                    modified_ns=excluded.modified_ns, device=excluded.device,
                    inode=excluded.inode,
                    has_verified_english_subtitle=excluded.has_verified_english_subtitle,
                    inspection_json=excluded.inspection_json,
                    last_scanned_at=CURRENT_TIMESTAMP
                WHERE media_files.size IS NOT excluded.size
                   OR media_files.modified_at IS NOT excluded.modified_at
                   OR media_files.modified_ns IS NOT excluded.modified_ns
                   OR media_files.device IS NOT excluded.device
                   OR media_files.inode IS NOT excluded.inode
                   OR media_files.has_verified_english_subtitle IS NOT excluded.has_verified_english_subtitle
                   OR media_files.inspection_json IS NOT excluded.inspection_json""",
                (relative_path, size, modified_at, modified_ns, device, inode,
                 verified, inspection_json),
            )
    _scan_write(writer, "record_scan", persist, media_path=relative_path)

def save_candidate(media_relative_path: str, media_size: int, media_modified_at: float,
                   candidate: dict, *, media_modified_ns: int | None = None,
                   media_device: int | None = None, media_inode: int | None = None,
                   writer=None) -> dict:
    def persist(db):
        db.execute("""INSERT INTO subtitle_candidates(
                media_relative_path, media_size, media_modified_at,
                media_modified_ns, media_device, media_inode, subtitle_id,
                file_id, file_name, language, format, release, moviehash_match,
                download_count, rating, trusted_uploader, hearing_impaired,
                ai_translated, match_score, match_reasons
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_relative_path, file_id) DO UPDATE SET
                media_size=excluded.media_size, media_modified_at=excluded.media_modified_at,
                media_modified_ns=excluded.media_modified_ns, media_device=excluded.media_device,
                media_inode=excluded.media_inode, subtitle_id=excluded.subtitle_id,
                file_name=CASE WHEN subtitle_candidates.status='staged'
                    THEN subtitle_candidates.file_name ELSE excluded.file_name END,
                language=excluded.language,
                format=CASE WHEN subtitle_candidates.status='staged'
                    THEN subtitle_candidates.format ELSE excluded.format END,
                release=excluded.release, moviehash_match=excluded.moviehash_match,
                download_count=excluded.download_count, rating=excluded.rating,
                trusted_uploader=excluded.trusted_uploader,
                hearing_impaired=excluded.hearing_impaired, ai_translated=excluded.ai_translated,
                match_score=excluded.match_score, match_reasons=excluded.match_reasons,
                status=CASE WHEN subtitle_candidates.status IN ('staged','rejected_non_english')
                    THEN subtitle_candidates.status ELSE 'discovered' END,
                review_decision=CASE WHEN
                    subtitle_candidates.media_size IS NOT excluded.media_size
                    OR subtitle_candidates.media_modified_at IS NOT excluded.media_modified_at
                    OR subtitle_candidates.media_modified_ns IS NOT excluded.media_modified_ns
                    OR subtitle_candidates.media_device IS NOT excluded.media_device
                    OR subtitle_candidates.media_inode IS NOT excluded.media_inode
                    THEN 'pending' ELSE subtitle_candidates.review_decision END,
                review_updated_at=CASE WHEN
                    subtitle_candidates.media_size IS NOT excluded.media_size
                    OR subtitle_candidates.media_modified_at IS NOT excluded.media_modified_at
                    OR subtitle_candidates.media_modified_ns IS NOT excluded.media_modified_ns
                    OR subtitle_candidates.media_device IS NOT excluded.media_device
                    OR subtitle_candidates.media_inode IS NOT excluded.media_inode
                    THEN NULL ELSE subtitle_candidates.review_updated_at END,
                patch_status=CASE WHEN (
                    subtitle_candidates.media_size IS NOT excluded.media_size
                    OR subtitle_candidates.media_modified_at IS NOT excluded.media_modified_at
                    OR subtitle_candidates.media_modified_ns IS NOT excluded.media_modified_ns
                    OR subtitle_candidates.media_device IS NOT excluded.media_device
                    OR subtitle_candidates.media_inode IS NOT excluded.media_inode
                ) AND subtitle_candidates.patch_status='patched'
                    THEN 'not_started' ELSE subtitle_candidates.patch_status END,
                patch_started_at=CASE WHEN (
                    subtitle_candidates.media_size IS NOT excluded.media_size
                    OR subtitle_candidates.media_modified_at IS NOT excluded.media_modified_at
                    OR subtitle_candidates.media_modified_ns IS NOT excluded.media_modified_ns
                    OR subtitle_candidates.media_device IS NOT excluded.media_device
                    OR subtitle_candidates.media_inode IS NOT excluded.media_inode
                ) AND subtitle_candidates.patch_status='patched'
                    THEN NULL ELSE subtitle_candidates.patch_started_at END,
                patch_completed_at=CASE WHEN (
                    subtitle_candidates.media_size IS NOT excluded.media_size
                    OR subtitle_candidates.media_modified_at IS NOT excluded.media_modified_at
                    OR subtitle_candidates.media_modified_ns IS NOT excluded.media_modified_ns
                    OR subtitle_candidates.media_device IS NOT excluded.media_device
                    OR subtitle_candidates.media_inode IS NOT excluded.media_inode
                ) AND subtitle_candidates.patch_status='patched'
                    THEN NULL ELSE subtitle_candidates.patch_completed_at END,
                updated_at=CURRENT_TIMESTAMP
            WHERE subtitle_candidates.media_size IS NOT excluded.media_size
               OR subtitle_candidates.media_modified_at IS NOT excluded.media_modified_at
               OR subtitle_candidates.media_modified_ns IS NOT excluded.media_modified_ns
               OR subtitle_candidates.media_device IS NOT excluded.media_device
               OR subtitle_candidates.media_inode IS NOT excluded.media_inode
               OR subtitle_candidates.subtitle_id IS NOT excluded.subtitle_id
               OR subtitle_candidates.language IS NOT excluded.language
               OR subtitle_candidates.release IS NOT excluded.release
               OR subtitle_candidates.moviehash_match IS NOT excluded.moviehash_match
               OR subtitle_candidates.download_count IS NOT excluded.download_count
               OR subtitle_candidates.rating IS NOT excluded.rating
               OR subtitle_candidates.trusted_uploader IS NOT excluded.trusted_uploader
               OR subtitle_candidates.hearing_impaired IS NOT excluded.hearing_impaired
               OR subtitle_candidates.ai_translated IS NOT excluded.ai_translated
               OR subtitle_candidates.match_score IS NOT excluded.match_score
               OR subtitle_candidates.match_reasons IS NOT excluded.match_reasons
               OR (subtitle_candidates.status!='staged' AND (
                    subtitle_candidates.file_name IS NOT excluded.file_name
                    OR subtitle_candidates.format IS NOT excluded.format))
               OR subtitle_candidates.status NOT IN ('discovered','staged','rejected_non_english')""",
            (
                media_relative_path, media_size, media_modified_at, media_modified_ns,
                media_device, media_inode, str(candidate.get("subtitle_id") or ""),
                candidate["file_id"], candidate["file_name"], candidate["language"],
                candidate["format"], candidate.get("release", ""),
                int(candidate.get("moviehash_match", False)), candidate.get("download_count", 0),
                candidate.get("rating"), int(candidate.get("trusted_uploader", False)),
                int(candidate.get("hearing_impaired", False)),
                int(candidate.get("ai_translated", False)), candidate.get("match_score", 0),
                json.dumps(candidate.get("match_reasons", [])),
            ),
        )
        row = db.execute(
            """SELECT id, status, staged_path, content_language,
                      content_language_confidence, content_status
               FROM subtitle_candidates WHERE media_relative_path=? AND file_id=?""",
            (media_relative_path, candidate["file_id"]),
        ).fetchone()
        return dict(row)
    return _scan_write(writer, "candidate_save", persist, media_path=media_relative_path)


def mark_candidate_staged(candidate_id: int, staged_path: str,
                          remaining: int | None = None, assessment: dict | None = None,
                          file_name: str | None = None, subtitle_format: str | None = None,
                          writer=None):
    content_language = (assessment or {}).get("content_language")
    content_confidence = (assessment or {}).get("content_confidence")
    content_status = (assessment or {}).get("content_status", "unknown")
    def persist(db):
        db.execute(
            """UPDATE subtitle_candidates SET status='staged', staged_path=?,
               provider_download_remaining=?, file_name=COALESCE(?, file_name),
               format=COALESCE(?, format), content_language=?,
               content_language_confidence=?, content_status=?, last_error=NULL,
               updated_at=CURRENT_TIMESTAMP WHERE id=? AND (
                 status IS NOT 'staged' OR staged_path IS NOT ? OR
                 provider_download_remaining IS NOT ? OR
                 content_language IS NOT ? OR content_language_confidence IS NOT ? OR
                 content_status IS NOT ? OR last_error IS NOT NULL OR
                 (? IS NOT NULL AND file_name IS NOT ?) OR
                 (? IS NOT NULL AND format IS NOT ?))""",
            (staged_path, remaining, file_name, subtitle_format,
             content_language, content_confidence, content_status, candidate_id,
             staged_path, remaining, content_language, content_confidence,
             content_status, file_name, file_name, subtitle_format, subtitle_format),
        )
    _scan_write(writer, "candidate_mark_staged", persist)


def mark_candidate_error(candidate_id: int, error: str, status: str = 'download_failed',
                         writer=None):
    normalized = error[:500]
    def persist(db):
        db.execute(
            """UPDATE subtitle_candidates SET status=?, last_error=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?
               AND status NOT IN ('staged', 'rejected_non_english')
               AND (status IS NOT ? OR last_error IS NOT ?)""",
            (status, normalized, candidate_id, status, normalized),
        )
    _scan_write(writer, "candidate_mark_error", persist)




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
               m.modified_ns AS current_media_modified_ns, m.device AS current_media_device,
               m.inode AS current_media_inode,
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
            or item.get("current_media_modified_ns") != item.get("media_modified_ns")
            or item.get("current_media_device") != item.get("media_device")
            or item.get("current_media_inode") != item.get("media_inode")
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




def reset_candidate_reviews_for_media(relative_path: str) -> None:
    """Require fresh approval after an existing-track-only media rewrite."""
    with connect() as db:
        db.execute(
            """UPDATE subtitle_candidates
               SET review_decision='pending', review_updated_at=NULL,
                   updated_at=CURRENT_TIMESTAMP
               WHERE media_relative_path=? AND patch_status!='running'""",
            (relative_path,),
        )


def record_media_patch(relative_path: str, status: str, details: dict,
                      backup_path: str | None = None) -> None:
    with connect() as db:
        db.execute(
            """INSERT INTO patch_history(
                   relative_path, subtitle_name, status, details, candidate_id, backup_path
               ) VALUES (?, ?, ?, ?, NULL, ?)""",
            (relative_path, "Existing subtitle track edit", status,
             json.dumps(details), backup_path),
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


def start_job(job_id: int, *, writer=None):
    def persist(db):
        db.execute(
            """UPDATE jobs SET status='running', started_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'""", (job_id,)
        )
    _scan_write(writer, "job_start", persist)


def update_job_progress(job_id: int, *, phase: str | None = None,
                        current_file: str | None = None, completed: int | None = None,
                        succeeded: int | None = None, failed: int | None = None,
                        total: int | None = None, writer=None):
    changed = [(key, value) for key, value in (
        ('phase', phase), ('current_file', current_file), ('completed', completed),
        ('succeeded', succeeded), ('failed', failed), ('total', total)
    ) if value is not None]
    if not changed:
        return
    assignments = [f"{key}=?" for key, _ in changed]
    conditions = [f"{key} IS NOT ?" for key, _ in changed]
    values = [value for _, value in changed]
    def persist(db):
        db.execute(
            f"UPDATE jobs SET updated_at=CURRENT_TIMESTAMP, {', '.join(assignments)} "
            f"WHERE id=? AND ({' OR '.join(conditions)})",
            values + [job_id] + values,
        )
    _scan_write(writer, "job_progress", persist)


def record_job_item(job_id: int, *, media_path: str, status: str, phase: str,
                    candidate_id: int | None = None, review_decision: str | None = None,
                    patch_status: str | None = None, error: str | None = None,
                    details: dict | None = None, writer=None):
    details_json = json.dumps(details or {})
    normalized_error = (error or '')[:500]
    def persist(db):
        db.execute(
            """INSERT INTO job_items(job_id, media_path, candidate_id, status, phase,
                   review_decision, patch_status, error, details_json, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                   CASE WHEN ?='running' THEN NULL ELSE CURRENT_TIMESTAMP END)
               ON CONFLICT(job_id, media_path) DO UPDATE SET
                   candidate_id=COALESCE(excluded.candidate_id, job_items.candidate_id),
                   status=excluded.status, phase=excluded.phase,
                   review_decision=COALESCE(excluded.review_decision, job_items.review_decision),
                   patch_status=COALESCE(excluded.patch_status, job_items.patch_status),
                   error=excluded.error, details_json=excluded.details_json,
                   completed_at=excluded.completed_at
               WHERE job_items.candidate_id IS NOT excluded.candidate_id
                  OR job_items.status IS NOT excluded.status
                  OR job_items.phase IS NOT excluded.phase
                  OR job_items.error IS NOT excluded.error
                  OR job_items.details_json IS NOT excluded.details_json
                  OR job_items.completed_at IS NOT excluded.completed_at""",
            (job_id, media_path, candidate_id, status, phase, review_decision,
             patch_status, normalized_error, details_json, status),
        )
    _scan_write(writer, "job_item", persist, media_path=media_path)


def finish_job(job_id: int, status: str, error: str | None = None, *, writer=None):
    if status not in {"completed", "failed", "cancelled", "interrupted"}:
        raise ValueError("Invalid terminal job state.")
    normalized_error = (error or '')[:500] or None
    def persist(db):
        db.execute(
            """UPDATE jobs SET status=?, phase=?, current_file=NULL, error=?,
               finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status IN ('queued','running')""",
            (status, status, normalized_error, job_id),
        )
    _scan_write(writer, "job_finish", persist)


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
                """UPDATE jobs SET status='interrupted', phase='interrupted',
                   current_file=NULL,
                   error='Application restarted before this job reached a terminal state.',
                   finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (row["id"],),
            )
        return len(rows)



def set_provider_status(status: str, message: str, *, error_code: str | None = None,
                        retry_at: str | None = None) -> None:
    """Persist a sanitized provider-wide state for the dashboard and retry worker."""
    with connect() as db:
        db.execute(
            """INSERT INTO provider_status(id, status, error_code, message, retry_at)
               VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                   error_code=excluded.error_code, message=excluded.message,
                   retry_at=excluded.retry_at, updated_at=CURRENT_TIMESTAMP""",
            (status, error_code, message[:300], retry_at),
        )


def get_provider_status() -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM provider_status WHERE id=1").fetchone()
    return dict(row) if row else None


def clear_provider_status() -> None:
    with connect() as db:
        db.execute("DELETE FROM provider_status WHERE id=1")
