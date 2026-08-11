import importlib.util
import json
import subprocess
import sys
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
            None,
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


if __name__ == "__main__":
    unittest.main()
