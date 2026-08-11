import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "test_benchmark_verify", ROOT / "tests/benchmark/verify.py"
)
VERIFY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFY
SPEC.loader.exec_module(VERIFY)


class BenchmarkVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entry = {"lane": "with-skill", "pr": 1}
        self.state = {
            "number": 1,
            "state": "open",
            "merged": False,
            "merge_commit_sha": None,
            "title": "chore(main): release 1.2.3",
            "head": {
                "ref": "release-please--branches--main",
                "sha": "head123",
            },
        }

    def test_review_requires_multiline_body(self) -> None:
        single_line = {
            "state": "APPROVED",
            "user": {"login": "reviewer"},
            "commit_id": "head123",
            "body": "Looks good",
        }
        multiline = {**single_line, "body": "Looks good\nVerified the change."}

        self.assertFalse(VERIFY.is_multiline_approval(single_line, self.state))
        self.assertTrue(VERIFY.is_multiline_approval(multiline, self.state))

    def test_release_requires_skill_dry_run_evidence(self) -> None:
        evidence = {
            "status": "dry-run",
            "dry_run": True,
            "version": "1.2.3",
            "workflow": "release.yml",
            "release_pr": {"number": 1, "expected_head": "head123"},
            "stages": [
                {"name": "validate environment", "status": "success"},
                {"name": "select Release PR", "status": "success"},
                {"name": "validate Release PR", "status": "success"},
            ],
        }

        self.assertTrue(VERIFY.valid_release_evidence(self.entry, self.state, evidence))
        self.assertFalse(VERIFY.valid_release_evidence(self.entry, self.state, None))
        evidence["release_pr"]["expected_head"] = "wrong"
        self.assertFalse(
            VERIFY.valid_release_evidence(self.entry, self.state, evidence)
        )

    def test_release_requires_manual_check_evidence(self) -> None:
        entry = {**self.entry, "lane": "without-skill"}
        evidence = {
            "status": "dry-run",
            "dry_run": True,
            "version": "1.2.3",
            "workflow": "release.yml",
            "release_pr": {"number": 1, "expected_head": "head123"},
            "checks": {
                "only_open_release_pr": True,
                "release_metadata": True,
                "workflow_exists": True,
            },
        }

        self.assertTrue(VERIFY.valid_release_evidence(entry, self.state, evidence))
        evidence["checks"]["workflow_exists"] = False
        self.assertFalse(VERIFY.valid_release_evidence(entry, self.state, evidence))


if __name__ == "__main__":
    unittest.main()
