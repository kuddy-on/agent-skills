import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


merge = load_module("test_gitea_merge", "skills/gitea-merge/scripts/merge.py")
release = load_module("test_gitea_release", "skills/gitea-release/scripts/release.py")
review = load_module("test_gitea_review", "skills/gitea-review/scripts/review_pr.py")


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class MergeTests(unittest.TestCase):
    def make_merger(self, login=None):
        return merge.Merger(Path.cwd(), "main", "auto", True, False, login)

    def test_explicit_login_must_match_origin_host(self):
        merger = self.make_merger("other")
        profiles = [{"name": "other", "url": "https://other.example"}]

        def command(args, check=True):
            if args[:4] == ["git", "remote", "get-url", "origin"]:
                return completed(args, stdout="git@gitea.example:owner/repo.git\n")
            if args[:4] == ["tea", "login", "list", "--output"]:
                return completed(args, stdout=json.dumps(profiles))
            self.fail(f"unexpected command: {args}")

        merger.command = command
        with self.assertRaisesRegex(merge.MergeError, "does not match origin host"):
            merger.resolve_repository()

    def test_merge_uses_atomic_expected_head(self):
        merger = self.make_merger()
        merger.login = "profile"
        merger.repo_slug = "owner/repo"
        commands = []
        merger.command = lambda args, check=True: (
            commands.append(args) or completed(args)
        )
        merger.get_pr = lambda number: {
            "number": number,
            "merged": True,
            "merge_commit_sha": "merged123",
        }

        result = merger.merge_pr(7, "rebase", "head123", True)

        self.assertTrue(result["merged"])
        self.assertIn("head_commit_id=head123", commands[0])
        self.assertIn("delete_branch_after_merge=true", commands[0])

    def test_api_accepts_list_responses(self):
        merger = self.make_merger()
        merger.command = lambda args, check=True: completed(args, stdout="[]")
        self.assertEqual(merger.api_for_login("/items", "profile"), [])

    def test_merge_rejects_changed_head(self):
        merger = self.make_merger()
        merger.login = "profile"
        merger.repo_slug = "owner/repo"
        merger.command = lambda args, check=True: completed(args, returncode=1)
        merger.get_pr = lambda number: {
            "number": number,
            "merged": False,
            "head": {"sha": "new-head"},
        }

        with self.assertRaisesRegex(merge.MergeError, "head changed"):
            merger.merge_pr(7, "rebase", "old-head", False)

    def test_cleanup_deletes_only_the_expected_local_ref(self):
        merger = self.make_merger()
        commands = []

        def command(args, check=True):
            commands.append(args)
            if args[:3] == ["git", "branch", "--show-current"]:
                return completed(args, stdout="main\n")
            if args[:3] == ["git", "show-ref", "--verify"]:
                return completed(args)
            if args[:2] == ["git", "rev-parse"]:
                return completed(args, stdout="head123\n")
            return completed(args)

        merger.command = command
        merger.cleanup("feature", "head123", True)

        self.assertIn(
            ["git", "update-ref", "-d", "refs/heads/feature", "head123"],
            commands,
        )

    def test_missing_merge_commit_sha_is_not_replaced_with_base_head(self):
        with self.assertRaisesRegex(merge.MergeError, "merge_commit_sha"):
            merge.require_merge_commit_sha({"merged": True}, 7)

    def test_distinct_conventional_scopes_rebase(self):
        merger = self.make_merger()
        merger.pr_commits = lambda number: [
            {"commit": {"message": "feat(api): first"}},
            {"commit": {"message": "test(core): second"}},
        ]
        self.assertEqual(merger.select_merge_strategy(7)["selected"], "rebase")

    def test_validation_prefetches_independent_gate_data_concurrently(self):
        merger = self.make_merger()
        active = 0
        peak = 0
        lock = threading.Lock()

        def api(endpoint):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            if endpoint.endswith("/status"):
                return {"state": "success"}
            if endpoint.endswith("/reviews"):
                return []
            if endpoint.endswith("/commits"):
                return [{"commit": {"message": "feat(api): test"}}]
            self.fail(f"unexpected endpoint: {endpoint}")

        merger.api = api
        merger.validate_pr(
            {
                "number": 7,
                "state": "open",
                "mergeable": True,
                "base": {"ref": "main"},
                "head": {"sha": "head123"},
            }
        )

        self.assertGreaterEqual(peak, 2)
        self.assertEqual(
            merger.pr_commits(7)[0]["commit"]["message"], "feat(api): test"
        )


class ReleaseTests(unittest.TestCase):
    def make_publisher(self, login=None):
        return release.Publisher(
            Path.cwd(),
            "main",
            "release.yml",
            "release-please--branches--main",
            1,
            10,
            True,
            login,
        )

    def test_release_merge_uses_atomic_expected_head(self):
        publisher = self.make_publisher()
        publisher.login = "profile"
        publisher.repo_slug = "owner/repo"
        commands = []
        publisher.command = lambda args, check=True: (
            commands.append(args) or completed(args)
        )
        states = iter(
            [
                {"number": 8, "merged": False, "head": {"sha": "head456"}},
                {
                    "number": 8,
                    "merged": True,
                    "merge_commit_sha": "release123",
                },
            ]
        )
        publisher.get_pr = lambda number: next(states)

        publisher.merge_release_pr(8, "head456")

        self.assertIn("head_commit_id=head456", commands[0])
        self.assertIn("delete_branch_after_merge=true", commands[0])

    def test_api_accepts_list_responses(self):
        publisher = self.make_publisher()
        publisher.command = lambda args, check=True: completed(args, stdout="[]")
        self.assertEqual(publisher.api_for_login("/items", "profile"), [])

    def test_release_requires_immutable_merge_sha(self):
        with self.assertRaisesRegex(release.ReleaseError, "merge_commit_sha"):
            release.require_merge_commit_sha({"merged": True}, 8)

    def test_network_error_is_not_downgraded_to_release_warning(self):
        publisher = self.make_publisher()
        publisher.api = lambda endpoint: (_ for _ in ()).throw(
            release.NetworkAccessRequired("network")
        )
        with self.assertRaises(release.NetworkAccessRequired):
            publisher.verify_release("1.2.3")

    def test_release_uses_one_admin_force_merge_after_gate_failure(self):
        publisher = self.make_publisher()
        publisher.login = "admin"
        publisher.repo_slug = "owner/repo"
        publisher.can_force_merge = True
        commands = []
        results = iter(
            [
                completed(
                    [],
                    stdout=json.dumps(
                        {"message": "Not all required status checks successful"}
                    ),
                    stderr="HTTP/1.1 405 Method Not Allowed\n",
                ),
                completed([]),
            ]
        )

        def command(args, check=True):
            commands.append(args)
            return next(results)

        states = iter(
            [
                {"number": 8, "merged": False, "head": {"sha": "head456"}},
                {
                    "number": 8,
                    "title": "chore(main): release 1.2.3",
                    "state": "open",
                    "merged": False,
                    "mergeable": True,
                    "base": {"ref": "main"},
                    "head": {
                        "ref": "release-please--branches--main",
                        "sha": "head456",
                    },
                },
                {
                    "number": 8,
                    "merged": True,
                    "merge_commit_sha": "release123",
                },
            ]
        )
        publisher.command = command
        publisher.get_pr = lambda number: next(states)
        publisher.api = lambda endpoint: {
            "branch_name": "main",
            "enable_status_check": True,
            "status_check_contexts": ["ci/test"],
            "required_approvals": 1,
        }

        result = publisher.merge_release_pr(8, "head456")

        self.assertTrue(result["merged"])
        self.assertIn("--include", commands[0])
        self.assertNotIn("force_merge=true", commands[0])
        self.assertIn("force_merge=true", commands[1])
        force_field = commands[1].index("force_merge=true")
        self.assertEqual(commands[1][force_field - 1], "--Field")
        self.assertTrue(publisher.summary["force_merge_used"])

    def test_release_refuses_force_merge_for_non_gate_failures(self):
        failures = (
            (500, "temporary backend failure"),
            (405, "rebase is not allowed for this repository"),
            (405, "Please try again later"),
            (405, "There are requested changes because policy lookup failed"),
        )
        for status, message in failures:
            with self.subTest(status=status, message=message):
                publisher = self.make_publisher()
                publisher.login = "admin"
                publisher.repo_slug = "owner/repo"
                publisher.can_force_merge = True
                commands = []

                def command(args, check=True):
                    commands.append(args)
                    return completed(
                        args,
                        stdout=json.dumps({"message": message}),
                        stderr=f"HTTP/1.1 {status} failure\n",
                    )

                states = iter(
                    [
                        {"number": 8, "merged": False},
                        {
                            "number": 8,
                            "title": "chore(main): release 1.2.3",
                            "state": "open",
                            "merged": False,
                            "mergeable": True,
                            "base": {"ref": "main"},
                            "head": {
                                "ref": "release-please--branches--main",
                                "sha": "head456",
                            },
                        },
                    ]
                )
                publisher.command = command
                publisher.get_pr = lambda number: next(states)

                with self.assertRaisesRegex(
                    release.ReleaseError, "recognized branch-protection gate"
                ):
                    publisher.merge_release_pr(8, "head456")

                self.assertEqual(len(commands), 1)
                self.assertNotIn("force_merge=true", commands[0])
                self.assertFalse(publisher.summary["force_merge_used"])

    def test_release_requires_both_ci_and_review_protection_for_force_merge(self):
        cases = (
            (
                {
                    "enable_status_check": True,
                    "status_check_contexts": ["ci/test"],
                    "required_approvals": 0,
                },
                "Not all required status checks successful",
            ),
            (
                {
                    "enable_status_check": False,
                    "status_check_contexts": [],
                    "required_approvals": 1,
                },
                "Does not have enough approvals",
            ),
        )
        for protection, message in cases:
            with self.subTest(protection=protection):
                publisher = self.make_publisher()
                publisher.login = "admin"
                publisher.repo_slug = "owner/repo"
                publisher.can_force_merge = True
                commands = []

                def command(args, check=True):
                    commands.append(args)
                    return completed(
                        args,
                        stdout=json.dumps({"message": message}),
                        stderr="HTTP/1.1 405 Method Not Allowed\n",
                    )

                states = iter(
                    [
                        {"number": 8, "merged": False},
                        {
                            "number": 8,
                            "title": "chore(main): release 1.2.3",
                            "state": "open",
                            "merged": False,
                            "mergeable": True,
                            "base": {"ref": "main"},
                            "head": {
                                "ref": "release-please--branches--main",
                                "sha": "head456",
                            },
                        },
                    ]
                )
                publisher.command = command
                publisher.get_pr = lambda number: next(states)
                publisher.api = lambda endpoint: protection

                with self.assertRaisesRegex(
                    release.ReleaseError, "both required CI status contexts"
                ):
                    publisher.merge_release_pr(8, "head456")

                self.assertEqual(len(commands), 1)
                self.assertFalse(publisher.summary["force_merge_used"])


class ReviewTests(unittest.TestCase):
    def test_explicit_login_must_match_pr_host(self):
        pull = review.PullRef("gitea.example", "owner", "repo", 1, "url")
        profiles = [{"name": "other", "url": "https://other.example"}]
        with patch.object(
            review.subprocess,
            "run",
            return_value=completed([], stdout=json.dumps(profiles)),
        ):
            with self.assertRaisesRegex(RuntimeError, "does not match PR host"):
                review.resolve_login(pull, "other")

    def test_pull_url_uses_hostname_without_port(self):
        pull = review.parse_pull_url("https://gitea.example:3000/owner/repo/pulls/1")
        self.assertEqual(pull.host, "gitea.example")

    def test_small_pr_fetches_only_first_file_page(self):
        pull = review.PullRef("gitea.example", "owner", "repo", 1, "url")

        class Client:
            def __init__(self):
                self.pull = pull
                self.endpoints = []

            def api_json(self, endpoint):
                self.endpoints.append(endpoint)
                return [{"filename": "app.py"}]

        client = Client()
        files = review.fetch_file_pages(client)

        self.assertEqual(files, [{"filename": "app.py"}])
        self.assertEqual(len(client.endpoints), 1)

    def test_prepare_packet_combines_snapshot_instructions_and_patch(self):
        pull = review.PullRef("gitea.example", "owner", "repo", 1, "url")
        snapshot = {
            "reviewer_login": "reviewer",
            "pr": {
                "url": "url",
                "head": {"sha": "head123", "ref": "feature"},
                "base": {"sha": "base123", "ref": "main"},
            },
            "stats": {"files": 1, "changed_lines": 2},
            "commits": [],
            "files": [
                {
                    "filename": "app.py",
                    "risk_tags": [],
                    "risk_score": 10,
                }
            ],
            "reviews": [],
            "issues": [],
            "instructions": {"AGENTS.md": "Review carefully."},
            "review_profile": {"lane": "fast"},
            "since_last_own_review": None,
            "cache": {},
            "commands": {},
        }
        diff = "diff --git a/app.py b/app.py\n+print('ok')\n"

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "full.diff").write_text(diff, encoding="utf-8")
            with (
                patch.object(review, "build_snapshot", return_value=snapshot),
                patch.object(review, "cache_dir_for", return_value=target),
            ):
                packet = review.build_review_packet(pull, "reviewer", False, 50_000)

        self.assertEqual(packet["instructions"]["AGENTS.md"], "Review carefully.")
        self.assertEqual(packet["initial_patch_files"], ["app.py"])
        self.assertIn("print('ok')", packet["initial_patch"])

    def test_review_profile_routes_reasoning_effort_by_scale(self):
        fast = review.choose_review_profile(
            {"files": 20, "changed_lines": 800, "high_risk_files": 2},
            None,
            "head123",
        )
        medium = review.choose_review_profile(
            {"files": 21, "changed_lines": 801, "high_risk_files": 3},
            None,
            "head123",
        )
        large = review.choose_review_profile(
            {"files": 51, "changed_lines": 3_001, "high_risk_files": 11},
            None,
            "head123",
        )
        focused = review.choose_review_profile(
            {"files": 100, "changed_lines": 10_000, "high_risk_files": 20},
            {"commit_id": "head123"},
            "head123",
        )

        self.assertEqual(
            (fast["lane"], fast["worker"]["reasoning_effort"]),
            ("fast", "medium"),
        )
        self.assertEqual(
            (medium["lane"], medium["worker"]["reasoning_effort"]),
            ("medium", "high"),
        )
        self.assertEqual(
            (large["lane"], large["worker"]["reasoning_effort"]),
            ("large", "xhigh"),
        )
        self.assertEqual(
            (focused["lane"], focused["worker"]["reasoning_effort"]),
            ("focused-rereview", "medium"),
        )
        self.assertEqual(
            {profile["worker"]["model"] for profile in (fast, medium, large, focused)},
            {"gpt-5.6-sol"},
        )


if __name__ == "__main__":
    unittest.main()
