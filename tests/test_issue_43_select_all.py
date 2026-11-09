import unittest
from pathlib import Path


class SelectAllCandidateDashboardTests(unittest.TestCase):
    def test_dashboard_exposes_select_all_and_clear_selection_controls(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('id="select-all-candidates"', source)
        self.assertIn('id="clear-candidate-selection"', source)
        self.assertIn('addEventListener("click", selectAllCandidates)', source)
        self.assertIn('addEventListener("click", clearCandidateSelection)', source)

    def test_select_all_only_targets_candidates_with_batch_selection_checkboxes(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('querySelectorAll(".candidate-select input[type=\'checkbox\']")', source)
        self.assertIn("selectedCandidateIds.add(Number(checkbox.value))", source)
        self.assertIn("checkbox.checked = true", source)
        self.assertIn("selectedCandidateIds.clear()", source)


if __name__ == "__main__":
    unittest.main()
