import unittest
from pathlib import Path

from app.main import candidate_should_be_visible


class PendingApprovalQueueTests(unittest.TestCase):
    def test_only_fresh_staged_pending_candidates_are_visible(self):
        base = {
            "status": "staged",
            "review_decision": "pending",
            "is_stale": False,
            "patch_status": "not_started",
        }
        self.assertTrue(candidate_should_be_visible(base))
        for field, value in (
            ("status", "discovered"),
            ("status", "download_failed"),
            ("review_decision", "approved"),
            ("review_decision", "rejected"),
            ("is_stale", True),
            ("patch_status", "running"),
            ("patch_status", "interrupted"),
            ("patch_status", "patched"),
        ):
            with self.subTest(field=field, value=value):
                self.assertFalse(candidate_should_be_visible({**base, field: value}))

    def test_dashboard_restores_folder_state_and_does_not_reload_historical_file_list(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('folderStateKey="subtitle-manager.folder-state.v1"', source)
        self.assertIn("persistFolderState()", source)
        self.assertIn("restoreFolderState()", source)
        self.assertNotIn('fetch("/api/media")', source)
        self.assertNotIn('def scanned_media():', source)
        self.assertIn('row.get("review_decision") == "pending"', source)
        self.assertIn('el("button", "Approve & inject")', source)

    def test_approval_action_requires_explicit_injection_confirmation(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("Approve this candidate and inject it", source)
        self.assertIn('"/api/candidates/"+id+"/inject"', source)
        self.assertIn('body:JSON.stringify({confirm:true,keep_subtitle_indices:selectedSubtitleIndices(id)})', source)


if __name__ == "__main__":
    unittest.main()
