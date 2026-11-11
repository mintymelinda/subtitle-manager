import tempfile
import threading
import unittest
from pathlib import Path

from app.config import settings
from app.database import connect
from app.scan_database_writer import ScanDatabaseWriter


class ScanDatabaseWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_database_path = settings.database_path
        settings.database_path = str(Path(self.temp.name) / "writer.sqlite3")
        with connect() as db:
            db.execute("CREATE TABLE writer_test (value INTEGER PRIMARY KEY)")

    def tearDown(self):
        settings.database_path = self.previous_database_path
        self.temp.cleanup()

    def test_concurrent_operations_share_batched_writer(self):
        writer = ScanDatabaseWriter(batch_window_seconds=0.05)
        writer.start()
        barrier = threading.Barrier(5)
        results = []
        failures = []

        def submit(value):
            try:
                barrier.wait()
                results.append(writer.submit(
                    "test_insert",
                    lambda db: db.execute(
                        "INSERT INTO writer_test(value) VALUES (?)", (value,)
                    ).rowcount,
                    media_path=f"file-{value}.mkv",
                ))
            except Exception as exc:
                failures.append(exc)

        threads = [
            threading.Thread(target=submit, args=(value,))
            for value in range(4)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        writer.close()

        self.assertEqual(failures, [])
        self.assertEqual(results, [1, 1, 1, 1])
        self.assertEqual(writer.stats["operations"], 4)
        self.assertEqual(writer.stats["max_batch_size"], 4)
        self.assertLess(writer.stats["batches"], 4)
        with connect() as db:
            values = [
                row[0] for row in db.execute(
                    "SELECT value FROM writer_test ORDER BY value"
                ).fetchall()
            ]
        self.assertEqual(values, [0, 1, 2, 3])

    def test_failed_operation_does_not_rollback_other_batch_members(self):
        writer = ScanDatabaseWriter(batch_window_seconds=0.05)
        writer.start()
        barrier = threading.Barrier(4)
        results = {}

        def submit(name, callback):
            barrier.wait()
            try:
                results[name] = writer.submit(name, callback)
            except Exception as exc:
                results[name] = exc

        callbacks = [
            ("first", lambda db: db.execute(
                "INSERT INTO writer_test(value) VALUES (1)"
            ).rowcount),
            ("broken", lambda _db: (_ for _ in ()).throw(ValueError("expected failure"))),
            ("last", lambda db: db.execute(
                "INSERT INTO writer_test(value) VALUES (2)"
            ).rowcount),
        ]
        threads = [
            threading.Thread(target=submit, args=entry) for entry in callbacks
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        writer.close()

        self.assertEqual(results["first"], 1)
        self.assertIsInstance(results["broken"], ValueError)
        self.assertEqual(results["last"], 1)
        with connect() as db:
            count = db.execute("SELECT COUNT(*) FROM writer_test").fetchone()[0]
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
