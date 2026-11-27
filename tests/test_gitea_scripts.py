import argparse
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
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

    def test_branch_cleanup_is_default_and_can_be_disabled(self):
        required = ["merge.py", "--repo", ".", "--pr", "7"]
        with patch.object(sys, "argv", required):
            self.assertTrue(merge.parse_args().cleanup)
        with patch.object(sys, "argv", [*required, "--keep-branch"]):
            self.assertFalse(merge.parse_args().cleanup)
        with patch.object(sys, "argv", [*required, "--cleanup"]):
            self.assertTrue(merge.parse_args().cleanup)

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

    def test_squash_merge_sets_conventional_commit_title(self):
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

        merger.merge_pr(
            7,
            "squash",
            "head123",
            True,
            "feat(api): consolidate endpoint work",
        )

        self.assertIn(
            "merge_title_field=feat(api): consolidate endpoint work",
            commands[0],
        )

    def test_squash_title_must_trigger_release(self):
        self.assertEqual(
            merge.validate_squash_title("fix: handle timeout"),
            "fix: handle timeout",
        )
        self.assertEqual(
            merge.validate_squash_title("refactor!: replace legacy API"),
            "refactor!: replace legacy API",
        )
        self.assertEqual(
            merge.validate_squash_title("perf: reduce latency"),
            "perf: reduce latency",
        )
        with self.assertRaisesRegex(merge.MergeError, "Conventional Commit"):
            merge.validate_squash_title("Handle timeout")
        with self.assertRaisesRegex(merge.MergeError, "release-triggering"):
            merge.validate_squash_title("chore: update tooling")

    def test_squash_title_comes_from_highest_impact_commit(self):
        title, source = merge.select_squash_title(
            [
                {"commit": {"message": "fix: handle timeout"}},
                {"commit": {"message": "feat(api): add endpoint\n\nDetails"}},
                {"commit": {"message": "fixup! feat(api): add endpoint"}},
            ],
            "Update the API",
        )

        self.assertEqual(title, "feat(api): add endpoint")
        self.assertEqual(source, "pull request commits")

    def test_squash_title_prefers_qualifying_pr_title(self):
        title, source = merge.select_squash_title(
            [{"commit": {"message": "feat: add endpoint"}}],
            "fix(api): handle timeout",
        )

        self.assertEqual(title, "fix(api): handle timeout")
        self.assertEqual(source, "pull request")

    def test_explicit_squash_title_overrides_pr_title(self):
        title, source = merge.select_squash_title(
            [{"commit": {"message": "feat: add endpoint"}}],
            "fix(api): handle timeout",
            "perf(api): reduce latency",
        )

        self.assertEqual(title, "perf(api): reduce latency")
        self.assertEqual(source, "explicit")

    def test_squash_title_rejects_commits_without_release_entry(self):
        with self.assertRaisesRegex(merge.MergeError, "no release-triggering"):
            merge.select_squash_title(
                [
                    {"commit": {"message": "chore: update tooling"}},
                    {"commit": {"message": "docs: update guide"}},
                ]
            )

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
            if "/reviews?" in endpoint or endpoint.endswith("/reviews"):
                return []
            if "/commits?" in endpoint or endpoint.endswith("/commits"):
                return [{"commit": {"message": "feat(api): test"}}]
            self.fail(f"unexpected endpoint: {endpoint}")

        merger.api = api
        merger.validate_pr(
            {
                "number": 7,
                "state": "open",
                "mergeable": True,
                "base": {"ref": "main"},
                "head": {"ref": "feature/test", "sha": "head123"},
            }
        )

        self.assertGreaterEqual(peak, 2)
        self.assertEqual(
            merger.pr_commits(7)[0]["commit"]["message"], "feat(api): test"
        )

    def test_comment_and_pending_reviews_do_not_clear_request_changes(self):
        merger = self.make_merger()
        merger.api = lambda endpoint: [
            {
                "id": 10,
                "user": {"id": 1},
                "state": "REQUEST_CHANGES",
                "dismissed": False,
            },
            {"id": 11, "user": {"id": 1}, "state": "COMMENT", "dismissed": False},
            {"id": 12, "user": {"id": 1}, "state": "PENDING", "dismissed": False},
            {
                "id": 20,
                "user": {"id": 2},
                "state": "REQUEST_CHANGES",
                "dismissed": True,
            },
            {
                "id": 30,
                "user": {"id": 3},
                "state": "REQUEST_CHANGES",
                "dismissed": False,
            },
            {"id": 31, "user": {"id": 3}, "state": "APPROVED", "dismissed": False},
        ]

        states = merger.latest_review_states(7)

        self.assertEqual(states, {1: "REQUEST_CHANGES", 3: "APPROVED"})

    def test_reviews_and_commits_paginate_beyond_first_page(self):
        merger = self.make_merger()

        def api(endpoint):
            if "/reviews?" in endpoint and "page=1" in endpoint:
                return [
                    {"id": idx, "user": {"id": idx}, "state": "APPROVED"}
                    for idx in range(1, 51)
                ]
            if "/reviews?" in endpoint and "page=2" in endpoint:
                return [{"id": 51, "user": {"id": 99}, "state": "REQUEST_CHANGES"}]
            if "/commits?" in endpoint and "page=1" in endpoint:
                return [{"commit": {"message": f"feat(c{i}): item"}} for i in range(50)]
            if "/commits?" in endpoint and "page=2" in endpoint:
                return [{"commit": {"message": "fixup! feat(c0): item"}}]
            self.fail(f"unexpected endpoint: {endpoint}")

        merger.api = api

        self.assertEqual(merger.latest_review_states(7)[99], "REQUEST_CHANGES")
        self.assertEqual(merger.select_merge_strategy(7)["selected"], "squash")

    def test_validate_pr_rejects_release_please_draft_and_unconfirmed_mergeable(self):
        cases = (
            (
                {
                    "number": 7,
                    "state": "open",
                    "mergeable": True,
                    "base": {"ref": "main"},
                    "head": {
                        "ref": "release-please--branches--main",
                        "sha": "head123",
                    },
                },
                "Release Please PR",
            ),
            (
                {
                    "number": 7,
                    "state": "open",
                    "draft": True,
                    "mergeable": True,
                    "base": {"ref": "main"},
                    "head": {"ref": "feature/wip", "sha": "head123"},
                },
                "draft",
            ),
            (
                {
                    "number": 7,
                    "state": "open",
                    "mergeable": None,
                    "base": {"ref": "main"},
                    "head": {"ref": "feature/pending", "sha": "head123"},
                },
                "conflict status is not confirmed",
            ),
        )
        for pr, pattern in cases:
            with self.subTest(pattern=pattern):
                merger = self.make_merger()
                with self.assertRaisesRegex(merge.MergeError, pattern):
                    merger.validate_pr(pr)

    def test_protected_branch_skips_remote_and_local_cleanup(self):
        merger = self.make_merger()
        self.assertTrue(merger.is_protected_branch("develop"))
        merger.cleanup("develop", "head123", remote_cleanup_requested=False)
        self.assertEqual(
            merger.summary["cleanup"]["remote"],
            "skipped: feature branch is protected",
        )

    def test_cleanup_skips_branch_checked_out_in_another_worktree(self):
        merger = self.make_merger()
        commands = []

        def command(args, check=True):
            commands.append(args)
            if args[:3] == ["git", "branch", "--show-current"]:
                return completed(args, stdout="main\n")
            if args[:3] == ["git", "worktree", "list"]:
                return completed(
                    args,
                    stdout=(
                        f"worktree {Path.cwd().resolve()}\n"
                        "branch refs/heads/main\n\n"
                        "worktree /tmp/other-worktree\n"
                        "branch refs/heads/feature\n"
                    ),
                )
            return completed(args)

        merger.command = command
        merger.cleanup("feature", "head123", True)

        self.assertEqual(
            merger.summary["cleanup"]["local"],
            "skipped: feature branch is checked out in another worktree",
        )
        self.assertFalse(
            any(cmd[:2] == ["git", "update-ref"] for cmd in commands),
        )

    def test_select_merge_strategy_handles_empty_commit_message(self):
        merger = self.make_merger()
        merger.pr_commits = lambda number: [
            {"commit": {"message": ""}},
            {"commit": {"message": "feat(api): valid"}},
        ]
        with self.assertRaises(merge.DecisionRequired):
            merger.select_merge_strategy(7)

    def test_squash_title_promotes_breaking_footer_and_skips_temporary_pr_title(self):
        commits = [
            {"commit": {"message": "fix: small tweak"}},
            {
                "commit": {
                    "message": (
                        "refactor(api): drop legacy endpoint\n\n"
                        "BREAKING CHANGE: removed /v1/items"
                    )
                }
            },
        ]
        title, source = merge.select_squash_title(commits, "fix: WIP temporary")
        footers = merge.extract_breaking_change_footers(commits)

        self.assertEqual(title, "refactor(api)!: drop legacy endpoint")
        self.assertEqual(source, "pull request commits")
        self.assertEqual(footers, "BREAKING CHANGE: removed /v1/items")

    def test_resolve_repository_matches_case_insensitively_and_by_port(self):
        merger = self.make_merger()
        profiles = [
            {"name": "wrong-port", "url": "https://gitea.example:3001"},
            {"name": "right-port", "url": "https://gitea.example:3000"},
        ]

        def command(args, check=True):
            if args[:4] == ["git", "remote", "get-url", "origin"]:
                return completed(
                    args, stdout="https://gitea.example:3000/myorg/myrepo.git\n"
                )
            if args[:4] == ["tea", "login", "list", "--output"]:
                return completed(args, stdout=json.dumps(profiles))
            if args[:4] == ["tea", "api", "--login", "right-port"]:
                return completed(
                    args,
                    stdout=json.dumps(
                        {"full_name": "MyOrg/MyRepo", "permissions": {"push": True}}
                    ),
                )
            self.fail(f"unexpected command: {args}")

        merger.command = command
        merger.resolve_repository()

        self.assertEqual(merger.login, "right-port")
        self.assertEqual(merger.repo_slug, "MyOrg/MyRepo")


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

    def test_release_parses_included_tea_headers_from_stdout(self):
        result = completed(
            [],
            stdout=(
                "HTTP/1.1 405 Method Not Allowed\r\n"
                "Content-Type: application/json\r\n\r\n"
                '{"message":"Not all required status checks successful"}'
            ),
        )

        self.assertTrue(release.is_force_merge_gate_failure(result))
        self.assertEqual(
            release.format_api_failure(result),
            "HTTP 405: Not all required status checks successful",
        )

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
        api_endpoints = []

        def api(endpoint):
            api_endpoints.append(endpoint)
            if "/branch_protections/" in endpoint:
                return {
                    "branch_name": "main",
                    "enable_status_check": True,
                    "status_check_contexts": [],
                    "required_approvals": 1,
                }
            if "/status?limit=1" in endpoint:
                return {"state": "pending", "total_count": 0, "statuses": None}
            if "/pulls/8/reviews" in endpoint:
                return []
            self.fail(f"unexpected API endpoint: {endpoint}")

        publisher.api = api

        result = publisher.merge_release_pr(8, "head456")

        self.assertTrue(result["merged"])
        self.assertIn("--include", commands[0])
        self.assertNotIn("force_merge=true", commands[0])
        self.assertIn("force_merge=true", commands[1])
        force_field = commands[1].index("force_merge=true")
        self.assertEqual(commands[1][force_field - 1], "--Field")
        self.assertTrue(any("/commits/head456/status" in url for url in api_endpoints))
        self.assertTrue(publisher.summary["force_merge_used"])

    def test_release_refuses_force_merge_for_non_gate_failures(self):
        failures = (
            (500, "temporary backend failure"),
            (405, "rebase is not allowed for this repository"),
            (405, "Please try again later"),
            (405, "Does not have enough approvals"),
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
            {
                "enable_status_check": True,
                "status_check_contexts": ["ci/test"],
                "required_approvals": 0,
            },
            {
                "enable_status_check": False,
                "status_check_contexts": [],
                "required_approvals": 1,
            },
        )
        for protection in cases:
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
                        stdout=json.dumps(
                            {"message": ("Not all required status checks successful")}
                        ),
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
                    release.ReleaseError,
                    "enable both CI status checks and required approvals",
                ):
                    publisher.merge_release_pr(8, "head456")

                self.assertEqual(len(commands), 1)
                self.assertFalse(publisher.summary["force_merge_used"])

    def test_release_refuses_force_merge_when_head_has_any_ci_status(self):
        publisher = self.make_publisher()

        def api(endpoint):
            if "/branch_protections/" in endpoint:
                return {
                    "enable_status_check": True,
                    "status_check_contexts": [],
                    "required_approvals": 1,
                }
            if "/status?limit=1" in endpoint:
                return {
                    "state": "failure",
                    "total_count": 1,
                    "statuses": [{"context": "ci/test", "status": "failure"}],
                }
            self.fail(f"unexpected API endpoint: {endpoint}")

        publisher.api = api

        with self.assertRaisesRegex(release.ReleaseError, "no CI status results"):
            publisher.validate_release_force_merge_policy("head456")

    def test_release_refuses_force_merge_when_other_gates_are_masked_by_missing_ci(
        self,
    ):
        release_pr = {
            "number": 8,
            "title": "chore(main): release 1.2.3",
            "state": "open",
            "merged": False,
            "mergeable": True,
            "base": {"ref": "main"},
            "head": {"ref": "release-please--branches--main", "sha": "head456"},
        }
        scenarios = (
            (
                "request_changes",
                {
                    "enable_status_check": True,
                    "required_approvals": 1,
                },
                {
                    "/pulls/8/reviews": [
                        {
                            "id": 1,
                            "user": {"id": 2},
                            "state": "REQUEST_CHANGES",
                            "dismissed": False,
                        }
                    ]
                },
                "request-changes",
            ),
            (
                "official_review_request",
                {
                    "enable_status_check": True,
                    "required_approvals": 1,
                    "block_on_official_review_requests": True,
                },
                {
                    "/pulls/8/reviews": [
                        {
                            "id": 1,
                            "user": {"id": 2},
                            "state": "REQUEST_REVIEW",
                            "dismissed": False,
                        }
                    ]
                },
                "official review requests",
            ),
            (
                "outdated_branch",
                {
                    "enable_status_check": True,
                    "required_approvals": 1,
                    "block_on_outdated_branch": True,
                },
                {
                    "/pulls/8/reviews": [],
                    "/branches/main": {"commit": {"id": "base999"}},
                    "/compare/head456...base999": {"total_commits": 1},
                },
                "behind main",
            ),
            (
                "protected_files",
                {
                    "enable_status_check": True,
                    "required_approvals": 1,
                    "protected_file_patterns": ".gitea/**",
                },
                {
                    "/pulls/8/reviews": [],
                    "/pulls/8/files": [{"filename": ".gitea/workflows/release.yml"}],
                },
                "protected files",
            ),
            (
                "unsigned_commits",
                {
                    "enable_status_check": True,
                    "required_approvals": 1,
                    "require_signed_commits": True,
                },
                {
                    "/pulls/8/reviews": [],
                    "/pulls/8/commits": [
                        {"commit": {"verification": {"verified": False}}}
                    ],
                },
                "unsigned or unverified commits",
            ),
        )
        for name, protection, extra, pattern in scenarios:
            with self.subTest(scenario=name):
                publisher = self.make_publisher()

                def api(endpoint, protection=protection, extra=extra):
                    if "/branch_protections/" in endpoint:
                        return protection
                    if "/status?limit=1" in endpoint:
                        return {"state": "pending", "total_count": 0, "statuses": None}
                    for needle, payload in extra.items():
                        if needle in endpoint:
                            return payload
                    self.fail(f"unexpected API endpoint: {endpoint}")

                publisher.api = api
                with self.assertRaisesRegex(release.ReleaseError, pattern):
                    publisher.validate_release_force_merge_policy(
                        "head456", pr=release_pr
                    )

    def test_release_matches_wildcard_branch_protection_rule(self):
        publisher = self.make_publisher()

        def api(endpoint):
            if endpoint.endswith("/branch_protections/main"):
                raise release.ReleaseError("not found")
            if endpoint.endswith("/branch_protections"):
                return [
                    {
                        "rule_name": "release/*",
                        "enable_status_check": False,
                        "required_approvals": 0,
                        "priority": 1,
                    },
                    {
                        "rule_name": "m*",
                        "enable_status_check": True,
                        "required_approvals": 1,
                        "priority": 2,
                    },
                ]
            if "/status?limit=1" in endpoint:
                return {"state": "pending", "total_count": 0, "statuses": None}
            if "/pulls/8/reviews" in endpoint:
                return []
            self.fail(f"unexpected API endpoint: {endpoint}")

        publisher.api = api
        publisher.validate_release_force_merge_policy(
            "head456",
            pr={"number": 8, "draft": False},
        )

    def test_release_title_accepts_component_and_v_prefix(self):
        publisher = self.make_publisher()
        for title in (
            "chore(main): release 1.2.3",
            "chore(main): release v1.2.3",
            "chore(main): release agent-skills 1.2.3",
        ):
            with self.subTest(title=title):
                version = publisher.validate_release_pr(
                    {
                        "number": 8,
                        "state": "open",
                        "merged": False,
                        "mergeable": True,
                        "title": title,
                        "base": {"ref": "main"},
                        "head": {"ref": "release-please--branches--main"},
                    }
                )
                self.assertEqual(version, "1.2.3")

    def test_verify_tag_uses_gitea_api_and_validates_commit_sha(self):
        publisher = self.make_publisher()

        def api(endpoint):
            if endpoint.endswith("/tags/v1.2.3"):
                return {"name": "v1.2.3", "commit": {"sha": "release123"}}
            raise release.ReleaseError("not found")

        publisher.api = api
        self.assertEqual(
            publisher.verify_tag("1.2.3", expected_commit="release123"), "v1.2.3"
        )
        with self.assertRaisesRegex(release.ReleaseError, "expected other999"):
            publisher.verify_tag("1.2.3", expected_commit="other999")

    def test_verify_tag_respects_explicit_tag_prefix_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "release-please-config.json").write_text(
                json.dumps({"packages": {".": {"include-v-in-tag": False}}}),
                encoding="utf-8",
            )
            publisher = release.Publisher(
                repo,
                "main",
                "release.yml",
                "release-please--branches--main",
                1,
                10,
                True,
                None,
            )
            self.assertEqual(publisher.candidate_tags("1.2.3"), ["1.2.3", "v1.2.3"])

            prefixed = release.Publisher(
                repo,
                "main",
                "release.yml",
                "release-please--branches--main",
                1,
                10,
                True,
                None,
                tag_prefix="pkg-v",
            )
            self.assertEqual(prefixed.candidate_tags("1.2.3"), ["pkg-v1.2.3"])

    def test_workflow_runs_url_encodes_branch_with_slash(self):
        publisher = release.Publisher(
            Path.cwd(),
            "release/1.0",
            "release.yml",
            "release-please--branches--release/1.0",
            1,
            10,
            True,
            None,
        )
        endpoints = []

        def api(endpoint):
            endpoints.append(endpoint)
            return {"workflow_runs": []}

        publisher.api = api
        publisher.workflow_runs()
        self.assertIn("branch=release%2F1.0", endpoints[0])


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

    def test_prepare_packet_does_not_go_empty_on_oversized_first_patch(self):
        pull = review.PullRef("gitea.example", "owner", "repo", 1, "url")
        snapshot = {
            "reviewer_login": "reviewer",
            "pr": {"url": "url", "head": {"sha": "head123"}},
            "stats": {"files": 2, "changed_lines": 2},
            "commits": [],
            "files": [
                {
                    "filename": "large.py",
                    "risk_tags": [],
                    "risk_score": 20,
                },
                {
                    "filename": "small.py",
                    "risk_tags": [],
                    "risk_score": 10,
                },
            ],
            "reviews": [],
            "issues": [],
            "instructions": {},
            "review_profile": {"lane": "fast"},
            "since_last_own_review": None,
            "cache": {},
            "commands": {},
        }
        diff = (
            "diff --git a/large.py b/large.py\n+"
            + "+x" * 200
            + "\ndiff --git a/small.py b/small.py\n+ok\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "full.diff").write_text(diff, encoding="utf-8")
            with (
                patch.object(review, "build_snapshot", return_value=snapshot),
                patch.object(review, "cache_dir_for", return_value=target),
            ):
                packet = review.build_review_packet(pull, "reviewer", False, 100)

        self.assertIn("small.py", packet["initial_patch_files"])
        self.assertIn("large.py", packet["initial_patch_omitted_files"])
        self.assertTrue(packet["initial_patch"])
        self.assertTrue(packet["initial_patch_limited"])

    def test_prepare_packet_truncates_when_every_patch_is_oversized(self):
        pull = review.PullRef("gitea.example", "owner", "repo", 1, "url")
        snapshot = {
            "reviewer_login": "reviewer",
            "pr": {"url": "url", "head": {"sha": "head123"}},
            "stats": {"files": 1, "changed_lines": 1},
            "commits": [],
            "files": [{"filename": "large.py", "risk_tags": [], "risk_score": 20}],
            "reviews": [],
            "issues": [],
            "instructions": {},
            "review_profile": {"lane": "fast"},
            "since_last_own_review": None,
            "cache": {},
            "commands": {},
        }
        diff = "diff --git a/large.py b/large.py\n" + "+x" * 200 + "\n"

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "full.diff").write_text(diff, encoding="utf-8")
            with (
                patch.object(review, "build_snapshot", return_value=snapshot),
                patch.object(review, "cache_dir_for", return_value=target),
            ):
                packet = review.build_review_packet(pull, "reviewer", False, 80)

        self.assertTrue(packet["initial_patch"])
        self.assertLessEqual(len(packet["initial_patch"]), 80)
        self.assertEqual(packet["initial_patch_truncated_files"], ["large.py"])
        self.assertEqual(packet["initial_patch_omitted_files"], ["large.py"])

    def test_bounded_review_history_retains_latest_own_review(self):
        own_review = {
            "id": 1,
            "reviewer": "reviewer",
            "commit_id": "head123",
        }
        reviews = [own_review] + [
            {"id": number, "reviewer": f"other-{number}", "commit_id": "head123"}
            for number in range(2, 8)
        ]

        bounded = review.bounded_review_history(reviews, "reviewer")
        profile = review.choose_review_profile(
            {}, review.latest_own_review(bounded, "reviewer"), "head123"
        )

        self.assertIn(own_review, bounded)
        self.assertEqual(profile["lane"], "focused-rereview")

    def test_review_profile_routes_capability_tiers_without_model_overrides(self):
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
            (fast["lane"], fast["worker"]),
            ("fast", {"capability_tier": "standard"}),
        )
        self.assertEqual(
            (medium["lane"], medium["worker"]),
            ("medium", {"capability_tier": "advanced"}),
        )
        self.assertEqual(
            (large["lane"], large["worker"]),
            ("large", {"capability_tier": "highest"}),
        )
        self.assertEqual(
            (focused["lane"], focused["worker"]),
            ("focused-rereview", {"capability_tier": "standard"}),
        )

    def test_each_size_dimension_can_raise_the_capability_tier(self):
        for field, value, tier in (
            ("files", 21, "advanced"),
            ("changed_lines", 801, "advanced"),
            ("high_risk_files", 3, "advanced"),
            ("files", 51, "highest"),
            ("changed_lines", 3_001, "highest"),
            ("high_risk_files", 11, "highest"),
        ):
            with self.subTest(field=field, value=value):
                profile = review.choose_review_profile({field: value}, None, "head123")
                self.assertEqual(profile["worker"]["capability_tier"], tier)

    def test_split_diff_handles_spaces_octal_utf8_quotes_renames_and_crlf(self):
        diff = (
            "diff --git a/file name.py b/file name.py\n"
            "--- a/file name.py\n"
            "+++ b/file name.py\n"
            "@@ -1 +1 @@\n+space\n"
            'diff --git "a/dir/\\344\\270\\255\\346\\226\\207.md" '
            '"b/dir/\\344\\270\\255\\346\\226\\207.md"\n'
            '--- "a/dir/\\344\\270\\255\\346\\226\\207.md"\n'
            '+++ "b/dir/\\344\\270\\255\\346\\226\\207.md"\n'
            "@@ -1 +1 @@\n+cn\n"
            "diff --git a/it's.py b/it's.py\n"
            "--- a/it's.py\n"
            "+++ b/it's.py\n"
            "@@ -1 +1 @@\n+quote\n"
            "diff --git a/normal.py b/normal.py\r\n"
            "--- a/normal.py\r\n"
            "+++ b/normal.py\r\n"
            "@@ -1 +1 @@\r\n+crlf\r\n"
            "diff --git a/empty name.txt b/empty name.txt\n"
            "new file mode 100644\n"
            "diff --git a/old name.py b/new name.py\n"
            "rename from old name.py\n"
            "rename to new name.py\n"
        )
        sections = review.split_diff(diff)

        self.assertIn("file name.py", sections)
        self.assertIn("dir/中文.md", sections)
        self.assertIn("it's.py", sections)
        self.assertIn("normal.py", sections)
        self.assertIn("empty name.txt", sections)
        self.assertIn("new name.py", sections)

    def test_extract_issue_refs_filters_noise_and_fetch_issues_tolerates_404(self):
        pull = review.PullRef("gitea.example", "owner", "repo", 1, "url")
        body = (
            "Color #000000 and URL https://example.com/a/b#1 and `#2`\n"
            "```\n#3\n```\n"
            "Fixes #42 and other/repo#7"
        )
        refs = review.extract_issue_refs(body, pull)
        self.assertEqual(refs, [("other", "repo", 7), ("owner", "repo", 42)])

        with patch.object(
            review.TeaClient,
            "api_json",
            side_effect=FileNotFoundError("/repos/owner/repo/issues/42"),
        ):
            self.assertEqual(
                review.fetch_issues("reviewer", [("owner", "repo", 42)], pull), []
            )

        with patch.object(
            review.TeaClient,
            "api_json",
            side_effect=RuntimeError("network_access_required: blocked"),
        ):
            with self.assertRaisesRegex(RuntimeError, "network_access_required"):
                review.fetch_issues("reviewer", [("owner", "repo", 42)], pull)

    def test_classify_pull_scores_changes_identically_to_snapshot_and_returns_login(
        self,
    ):
        pull = review.PullRef("gitea.example", "owner", "repo", 1, "url")

        def api_json(self_client, endpoint):
            if endpoint == "/user":
                return {"login": "reviewer"}
            if endpoint == "/repos/owner/repo/pulls/1":
                return {
                    "number": 1,
                    "head": {"ref": "feat", "sha": "head123"},
                    "base": {"ref": "main", "sha": "base123"},
                }
            if "/files?" in endpoint:
                return [
                    {"filename": "auth/a.py", "additions": 1, "deletions": 0},
                    {"filename": "auth/b.py", "additions": 1, "deletions": 0},
                    {
                        "filename": "src/api/handler.py",
                        "additions": 500,
                        "deletions": 0,
                    },
                ]
            if "/reviews?" in endpoint:
                return []
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        with patch.object(review.TeaClient, "api_json", api_json):
            result = review.classify_pull(pull, "my-login")

        self.assertEqual(result["login"], "my-login")
        self.assertEqual(result["stats"]["high_risk_files"], 3)
        self.assertEqual(result["review_profile"]["lane"], "medium")
        self.assertEqual(
            result["review_profile"]["worker"]["capability_tier"], "advanced"
        )

    def test_snapshot_invalidates_cache_when_pr_base_changes(self):
        pull = review.PullRef(
            "gitea.example",
            "owner",
            "repo",
            1,
            "https://gitea.example/owner/repo/pulls/1",
        )
        state = {"base_ref": "main", "base_sha": "base1", "diff_calls": 0}

        def api_json(self_client, endpoint):
            if endpoint == "/user":
                return {"login": "reviewer"}
            if endpoint == "/repos/owner/repo/pulls/1":
                return {
                    "number": 1,
                    "title": "PR",
                    "body": "",
                    "state": "open",
                    "head": {"ref": "feat", "sha": "head123"},
                    "base": {"ref": state["base_ref"], "sha": state["base_sha"]},
                }
            if "/commits?" in endpoint:
                return [{"sha": "head123", "commit": {"message": "feat: x"}}]
            if "/reviews?" in endpoint:
                return []
            if "/files?" in endpoint:
                return [
                    {
                        "filename": f"{state['base_ref']}.py",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                    }
                ]
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        def api_text(self_client, endpoint):
            if endpoint.endswith(".diff"):
                state["diff_calls"] += 1
                name = f"{state['base_ref']}.py"
                return (
                    f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n+ok\n"
                )
            return '{"message":"not found"}'

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "head123"
            with (
                patch.object(review, "cache_dir_for", return_value=cache_dir),
                patch.object(review.TeaClient, "api_json", api_json),
                patch.object(review.TeaClient, "api_text", api_text),
            ):
                first = review.build_snapshot(pull, "my-login", refresh=False)
                second = review.build_snapshot(pull, "my-login", refresh=False)
                state["base_ref"] = "release"
                state["base_sha"] = "base2"
                retargeted = review.build_snapshot(pull, "my-login", refresh=False)

        self.assertFalse(first["cache"]["reused_code_snapshot"])
        self.assertTrue(second["cache"]["reused_code_snapshot"])
        self.assertFalse(retargeted["cache"]["reused_code_snapshot"])
        self.assertEqual(state["diff_calls"], 2)
        self.assertEqual(retargeted["files"][0]["filename"], "release.py")
        self.assertFalse(first["commands"]["submit"].startswith("python3 "))
        self.assertIn("--login my-login", first["commands"]["submit"])
        self.assertIn("--head head123", first["commands"]["show_files"])

    def test_submit_review_uses_reviews_api_with_commit_id_and_no_retry(self):
        commands = []
        retries_used = []

        def fake_run(self_client, *args):
            commands.append(args)
            retries_used.append(self_client.retries)
            if args[:2] == ("api", "--login") and "--method" in args:
                return '{"id": 22}'
            endpoint = args[-1]
            if endpoint == "/user":
                return json.dumps({"login": "reviewer"})
            if endpoint == "/repos/owner/repo/pulls/1":
                return json.dumps({"head": {"ref": "feat", "sha": "head123"}})
            if "/reviews?" in endpoint:
                return json.dumps(
                    [
                        {
                            "id": 21,
                            "user": {"login": "other"},
                            "state": "COMMENT",
                            "commit_id": "head123",
                            "body": "note",
                        },
                        {
                            "id": 22,
                            "user": {"login": "reviewer"},
                            "state": "APPROVED",
                            "commit_id": "head123",
                            "body": "--help\nVerified line two",
                        },
                    ]
                )
            raise AssertionError(f"unexpected args: {args}")

        ns = argparse.Namespace(
            url="https://gitea.example/owner/repo/pulls/1",
            login="rev-login",
            expected_head="head123",
            state="approve",
            body="--help\nVerified line two",
            body_file=None,
            dry_run=False,
        )
        stdout = io.StringIO()
        with (
            patch.object(review.TeaClient, "run", fake_run),
            redirect_stdout(stdout),
        ):
            review.submit_review(ns)

        post_cmd = next(cmd for cmd in commands if "--method" in cmd)
        post_index = commands.index(post_cmd)
        self.assertEqual(retries_used[post_index], 0)
        self.assertIn("event=APPROVED", post_cmd)
        self.assertIn("commit_id=head123", post_cmd)
        self.assertIn("body=--help\nVerified line two", post_cmd)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["latest_review"]["id"], 22)
        self.assertTrue(payload["multiline_body"])

    def test_show_cached_truncates_oversized_single_file_and_supports_head(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "head123"
            cache_dir.mkdir(parents=True)
            (cache_dir / "bundle.json").write_text(
                json.dumps({"files": [{"filename": "big.py", "risk_score": 40}]}),
                encoding="utf-8",
            )
            (cache_dir / "full.diff").write_text(
                "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
                + "+x" * 200
                + "\n",
                encoding="utf-8",
            )
            ns = argparse.Namespace(
                url="https://gitea.example/owner/repo/pulls/1",
                head="head123",
                files=["big.py"],
                review_set=False,
                risk="high",
                instructions=False,
                max_chars=120,
            )
            stdout = io.StringIO()
            with (
                patch.object(review, "cache_dir_for", return_value=cache_dir),
                redirect_stdout(stdout),
            ):
                review.show_cached(ns)

        output = stdout.getvalue()
        self.assertTrue(output)
        self.assertIn("truncated at 120 chars", output)

    def test_parse_pull_url_supports_subpath_tabs_and_case_insensitive_login(self):
        pull = review.parse_pull_url(
            "https://gitea.example:3000/gitea/MyOrg/MyRepo/pulls/12/files"
        )
        self.assertEqual(pull.host, "gitea.example")
        self.assertEqual(pull.port, 3000)
        self.assertEqual(pull.slug, "MyOrg/MyRepo")
        self.assertEqual(pull.index, 12)
        self.assertEqual(
            pull.url, "https://gitea.example:3000/gitea/MyOrg/MyRepo/pulls/12"
        )

        profiles = [
            {"name": "p3001", "url": "https://gitea.example:3001"},
            {"name": "p3000", "url": "https://gitea.example:3000/gitea"},
        ]

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["tea", "login", "list"]:
                return completed(cmd, stdout=json.dumps(profiles))
            if cmd[:4] == ["tea", "api", "--login", "p3000"]:
                return completed(cmd, stdout=json.dumps({"full_name": "myorg/myrepo"}))
            raise AssertionError(f"unexpected cmd: {cmd}")

        with patch.object(review.subprocess, "run", fake_run):
            self.assertEqual(review.resolve_login(pull, None), "p3000")


if __name__ == "__main__":
    unittest.main()
