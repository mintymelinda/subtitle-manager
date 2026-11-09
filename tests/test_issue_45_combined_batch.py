import unittest
from pathlib import Path


class CombinedBatchInjectionTests(unittest.TestCase):
    def test_dashboard_has_one_combined_batch_action(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('id="batch-run"', source)
        self.assertIn("Approve & patch selected", source)
        self.assertNotIn('id="batch-approve"', source)
        self.assertNotIn('id="batch-inject"', source)

    def test_batch_approves_pending_candidates_before_injection(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('candidate.review_decision !== "approved"', source)
        self.assertIn('JSON.stringify({decision: "approved"})', source)
        self.assertIn('"/api/candidates/" + id + "/inject"', source)

    def test_batch_updates_job_progress_until_each_injection_finishes(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("waitForBatchInjection", source)
        self.assertIn('fetch("/api/jobs/" + jobId', source)
        self.assertIn('await new Promise(resolve => setTimeout(resolve, 1000))', source)
        self.assertIn('"Batch injection · " + index + "/" + total', source)


if __name__ == "__main__":
    unittest.main()
