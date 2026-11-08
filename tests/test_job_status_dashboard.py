from pathlib import Path
import unittest


class JobStatusDashboardTests(unittest.TestCase):
    def test_job_polling_handles_non_json_responses_without_false_completion(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("async function readJsonResponse(response,context)", source)
        self.assertIn("const body=await response.text()", source)
        self.assertIn("JSON.parse(body)", source)
        self.assertIn("returned non-JSON (HTTP ", source)
        self.assertIn('headers:{"Accept":"application/json"}', source)
        self.assertIn('readJsonResponse(r,"Job history request")', source)
        self.assertIn('readJsonResponse(detailResponse,"Job detail request")', source)

    def test_job_polling_retries_and_exposes_manual_retry(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("if(failures<5)", source)
        self.assertIn("Math.min(1000*Math.pow(2,failures-1),8000)", source)
        self.assertIn('el("button","Retry status check")', source)
        self.assertIn("may still be running", source)


    def test_dashboard_limits_history_and_hides_successful_per_file_scan_entries(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/jobs?limit=10"', source)
        self.assertIn('if(j.kind==="scan"&&item.status==="completed")continue;', source)

    def test_candidate_queue_filters_stale_rejected_and_completed_results(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("def candidate_should_be_visible(row: dict) -> bool:", source)
        self.assertIn('row.get("review_decision") != "rejected"', source)
        self.assertIn('row.get("patch_status") != "patched"', source)
        self.assertIn('rows = [row for row in rows if candidate_should_be_visible(row)]', source)

if __name__ == "__main__":
    unittest.main()
