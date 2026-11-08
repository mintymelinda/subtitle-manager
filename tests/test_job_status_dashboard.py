from pathlib import Path
import unittest

from app.main import candidate_should_be_visible


class JobStatusDashboardTests(unittest.TestCase):
    def test_job_polling_handles_non_json_responses_without_false_completion(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("async function readJsonResponse(response,context)", source)
        self.assertIn("const body=await response.text()", source)
        self.assertIn("JSON.parse(body)", source)
        self.assertIn("returned non-JSON (HTTP ", source)
        self.assertIn('headers:{"Accept":"application/json"}', source)
        self.assertNotIn('readJsonResponse(r,"Job history request")', source)
        self.assertIn('readJsonResponse(r,"Job status request")', source)
        self.assertIn('fetch("/api/jobs/"+id,{headers:{"Accept":"application/json"}})', source)
        self.assertIn('if(["completed","failed","cancelled","interrupted"].includes(j.status))', source)

    def test_job_polling_retries_and_exposes_manual_retry(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("if(failures<5)", source)
        self.assertIn("Math.min(1000*Math.pow(2,failures-1),8000)", source)
        self.assertIn('el("button","Retry status check")', source)
        self.assertIn("may still be running", source)


    def test_candidate_visibility_keeps_only_current_actionable_or_recovery_items(self):
        base = {
            "status": "staged",
            "is_stale": False,
            "review_decision": "pending",
            "patch_status": "not_started",
        }
        self.assertTrue(candidate_should_be_visible(base))
        self.assertFalse(candidate_should_be_visible({**base, "is_stale": True}))
        self.assertFalse(candidate_should_be_visible({**base, "review_decision": "rejected"}))
        self.assertFalse(candidate_should_be_visible({**base, "patch_status": "patched"}))
        self.assertTrue(candidate_should_be_visible({**base, "patch_status": "interrupted", "is_stale": True}))
        self.assertTrue(candidate_should_be_visible({**base, "patch_status": "running"}))
        self.assertFalse(candidate_should_be_visible({**base, "status": "download_failed"}))

    def test_dashboard_removes_recent_activity_history_but_keeps_active_progress(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("Recent activity and processing history", source)
        self.assertNotIn('fetch("/api/jobs?limit=10"', source)
        self.assertNotIn("refreshJobHistory", source)
        self.assertIn('id="job-progress"', source)
        self.assertIn('readJsonResponse(r,"Job status request")', source)

    def test_candidate_queue_filters_stale_rejected_and_completed_results(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("def candidate_should_be_visible(row: dict) -> bool:", source)
        self.assertIn('row.get("review_decision") != "rejected"', source)
        self.assertIn('row.get("patch_status") != "patched"', source)
        self.assertIn('rows = [row for row in rows if candidate_should_be_visible(row)]', source)

if __name__ == "__main__":
    unittest.main()
