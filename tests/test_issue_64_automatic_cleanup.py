import unittest
from pathlib import Path


class AutomaticSubtitleCleanupDashboardTests(unittest.TestCase):
    def test_select_none_reveals_a_separate_explicit_cleanup_action(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('el("button","Run automatic cleanup")', source)
        self.assertIn("run.hidden=!checked", source)
        self.assertIn('automaticCheckbox.checked=false', source)
        self.assertIn('automaticCheckbox.setAttribute("aria-label","Select None — enable automatic subtitle cleanup")', source)

    def test_cleanup_uses_first_valid_staged_candidate_and_removes_existing_tracks(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('const first=options[0]', source)
        self.assertIn('item.status==="staged"&&!item.is_stale', source)
        self.assertIn('["pending","approved"].includes(item.review_decision)', source)
        self.assertIn('JSON.stringify({confirm:true,keep_subtitle_indices:[]})', source)

    def test_cleanup_has_one_explicit_confirmation_before_silent_injection(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('if(!confirm("Run automatic cleanup for "+candidate.file_name', source)
        self.assertIn('body:JSON.stringify({decision:"approved"})', source)
        self.assertIn('trackJob(injected.job_id)', source)
        self.assertIn('No further warnings or prompts will appear.', source)


if __name__ == "__main__":
    unittest.main()
