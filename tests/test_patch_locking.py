import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.patcher import patch_mkv


class PatchLockingTests(unittest.TestCase):
    def test_different_candidates_for_same_media_are_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.mkv"
            source.write_bytes(b"media")
            active = 0
            maximum_active = 0
            guard = threading.Lock()
            entered = threading.Event()

            def fake_patch(*_args, **_kwargs):
                nonlocal active, maximum_active
                with guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                    entered.set()
                time.sleep(0.2)
                with guard:
                    active -= 1
                return {"status": "patched"}

            results = []
            with patch("app.patcher._patch_mkv_unlocked", side_effect=fake_patch):
                first = threading.Thread(target=lambda: results.append(
                    patch_mkv(source, Path("candidate-one.srt"), approved=True)))
                second = threading.Thread(target=lambda: results.append(
                    patch_mkv(source, Path("candidate-two.srt"), approved=True)))
                first.start()
                self.assertTrue(entered.wait(2))
                second.start()
                first.join(3)
                second.join(3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(len(results), 2)
            self.assertEqual(maximum_active, 1)


if __name__ == "__main__":
    unittest.main()
