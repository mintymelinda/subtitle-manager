import threading

from app import database
from app.config import settings


def test_database_uses_wal_and_busy_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "subtitle-manager.sqlite3"))

    database.initialize()

    with database.connect() as db:
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
        foreign_keys = db.execute("PRAGMA foreign_keys").fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert busy_timeout == database.SQLITE_BUSY_TIMEOUT_MS
    assert foreign_keys == 1


def test_dashboard_reads_continue_during_worker_write_transaction(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "subtitle-manager.sqlite3"))
    database.initialize()
    job = database.create_job("scan", total=1)

    writer_ready = threading.Event()
    release_writer = threading.Event()
    writer_errors = []

    def hold_write_transaction():
        try:
            with database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE jobs SET phase=? WHERE id=?",
                    ("worker-writing", job["id"]),
                )
                writer_ready.set()
                assert release_writer.wait(timeout=5)
        except Exception as exc:  # surfaced in the test thread below
            writer_errors.append(exc)

    writer = threading.Thread(target=hold_write_transaction)
    writer.start()
    try:
        assert writer_ready.wait(timeout=5)
        # This is the same read path used by /api/jobs and /api/jobs/{id}.
        assert database.list_jobs()[0]["id"] == job["id"]
        assert database.get_job(job["id"])["phase"] == "worker-writing" or (
            database.get_job(job["id"])["phase"] is None
        )
    finally:
        release_writer.set()
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []


def test_write_contention_waits_instead_of_failing_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "subtitle-manager.sqlite3"))
    database.initialize()
    job = database.create_job("scan", total=1)

    writer_ready = threading.Event()
    release_writer = threading.Event()
    writer_errors = []

    def hold_write_transaction():
        try:
            with database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                writer_ready.set()
                assert release_writer.wait(timeout=5)
        except Exception as exc:
            writer_errors.append(exc)

    writer = threading.Thread(target=hold_write_transaction)
    writer.start()
    try:
        assert writer_ready.wait(timeout=5)
        # Keep the first writer open briefly. The second writer must wait for
        # SQLite's busy timeout machinery rather than fail immediately.
        release_timer = threading.Timer(0.15, release_writer.set)
        release_timer.start()
        database.update_job_progress(job["id"], phase="after-contention")
        release_timer.join(timeout=2)
    finally:
        release_writer.set()
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []
    assert database.get_job(job["id"])["phase"] == "after-contention"
