"""A single, batched SQLite writer for background scan mutations."""
from __future__ import annotations

import sqlite3
import time
from concurrent.futures import Future
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread

from app.config import settings
from app.observability import logger, timed_stage


class ScanDatabaseWriter:
    """Serialize scan writes on one connection and commit small batches."""

    def __init__(self, batch_size: int = 64, batch_window_seconds: float = 0.015):
        self.batch_size = max(1, batch_size)
        self.batch_window_seconds = max(0.0, batch_window_seconds)
        self._queue = Queue()
        self._ready = Event()
        self._thread = None
        self._startup_error = None
        self._closed = False
        self.stats = {"batches": 0, "operations": 0, "max_batch_size": 0}

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Scan database writer already started.")
        self._thread = Thread(target=self._run, name="scan-database-writer", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._startup_error:
            raise RuntimeError("Could not start scan database writer.") from self._startup_error

    def submit(self, operation, callback, *, media_path=None):
        if self._thread is None or self._closed:
            raise RuntimeError("Scan database writer is not running.")
        future = Future()
        self._queue.put({
            "operation": operation,
            "callback": callback,
            "media_path": media_path,
            "queued_at": time.perf_counter(),
            "future": future,
        })
        return future.result()

    def close(self):
        if self._thread is None or self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join()
        self._thread = None

    def _connect(self):
        path = Path(settings.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with timed_stage("scan_db_connection_setup"):
            db = sqlite3.connect(path, timeout=30, check_same_thread=True)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA foreign_keys=ON")
        return db

    def _process_batch(self, db, batch):
        for item in batch:
            logger.info(
                "stage_timing",
                extra={
                    "event": "stage_timing",
                    "stage": "scan_db_queue_wait",
                    "elapsed_ms": round((time.perf_counter() - item["queued_at"]) * 1000, 2),
                    "media_path": item["media_path"],
                    "database_operation": item["operation"],
                },
            )
        # BEGIN IMMEDIATE obtains SQLite's writer reservation; time it separately.
        with timed_stage("scan_db_lock_wait", batch_size=len(batch)):
            db.execute("BEGIN IMMEDIATE")

        results = []
        for index, item in enumerate(batch):
            savepoint = f"scan_write_{index}"
            db.execute(f"SAVEPOINT {savepoint}")
            try:
                with timed_stage(
                    "scan_db_sql",
                    media_path=item["media_path"],
                    database_operation=item["operation"],
                ):
                    value = item["callback"](db)
                db.execute(f"RELEASE SAVEPOINT {savepoint}")
                results.append((item, value, None))
            except Exception as exc:
                db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                db.execute(f"RELEASE SAVEPOINT {savepoint}")
                results.append((item, None, exc))

        try:
            with timed_stage("scan_db_commit", batch_size=len(batch)):
                db.commit()
        except Exception as exc:
            db.rollback()
            for item, _value, operation_error in results:
                item["future"].set_exception(operation_error or exc)
            return

        for item, value, error in results:
            if error is None:
                item["future"].set_result(value)
            else:
                item["future"].set_exception(error)
        self.stats["batches"] += 1
        self.stats["operations"] += len(batch)
        self.stats["max_batch_size"] = max(self.stats["max_batch_size"], len(batch))
        logger.info(
            "scan_db_batch_completed",
            extra={
                "event": "scan_db_batch_completed",
                "batch_size": len(batch),
                "operation_count": len(batch),
                "queue_depth": self._queue.qsize(),
            },
        )

    def _run(self):
        db = None
        try:
            db = self._connect()
            self._ready.set()
            stopping = False
            while not stopping:
                first = self._queue.get()
                if first is None:
                    break
                batch = [first]
                deadline = time.perf_counter() + self.batch_window_seconds
                while len(batch) < self.batch_size:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break
                    try:
                        item = self._queue.get(timeout=remaining)
                    except Empty:
                        break
                    if item is None:
                        stopping = True
                        break
                    batch.append(item)
                try:
                    self._process_batch(db, batch)
                except Exception as exc:
                    db.rollback()
                    for item in batch:
                        if not item["future"].done():
                            item["future"].set_exception(exc)
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            while True:
                try:
                    item = self._queue.get_nowait()
                except Empty:
                    break
                if item is not None and not item["future"].done():
                    item["future"].set_exception(exc)
        finally:
            if db is not None:
                with timed_stage("scan_db_connection_close"):
                    db.close()
