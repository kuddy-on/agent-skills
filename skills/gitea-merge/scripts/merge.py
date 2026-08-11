#!/usr/bin/env python3
"""Deterministically validate and merge one ordinary Gitea pull request."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

CONVENTIONAL_TITLE = re.compile(
    r"^(?P<type>feat|fix|perf|refactor|docs|test|build|ci|chore|style|revert)"
    r"(?:\((?P<scope>[^)]+)\))?!?:\s+\S+"
)
TEMPORARY_TITLE = re.compile(
    r"(?:^fixup!|^squash!|\bWIP\b|debug|临时|修复(?:测试|CI)|"
    r"address(?:ing)? review|review (?:fix|change|adjustment))",
    re.IGNORECASE,
)
ISSUE_REFERENCE = re.compile(r"(?<![\w/])#(\d+)\b")
RELEASE_BRANCH_PREFIX = "release-please--branches--"
NETWORK_ERRORS = (
    "connection refused",
    "connection reset",
    "could not resolve",
    "dial tcp",
    "i/o timeout",
    "network is unreachable",
    "no such host",
    "temporary failure",
    "tls handshake timeout",
    "context deadline exceeded",
)


class MergeError(RuntimeError):
    pass


class DecisionRequired(MergeError):
    pass


class NetworkAccessRequired(MergeError):
    pass


class Merger:
    def __init__(
        self,
        repo: Path,
        base: str,
        merge_strategy: str,
        dry_run: bool,
        skip_ci_check: bool,
        login: str | None,
    ) -> None:
        self.repo = repo
        self.base = base
        self.requested_merge_strategy = merge_strategy
        self.dry_run = dry_run
        self.skip_ci_check = skip_ci_check
        self.requested_login = login
        self.login: str | None = None
        self.owner: str | None = None
        self.repo_name: str | None = None
        self.repo_slug: str | None = None
        self._commit_cache: dict[int, list[dict[str, Any]]] = {}
        self.summary: dict[str, Any] = {
            "repository": None,
            "login": None,
            "base": base,
            "status": "running",
            "dry_run": dry_run,
            "pr": None,
            "merge_strategy": None,
            "merge_commit_sha": None,
            "cleanup": None,
            "stages": [],
        }

    def log(self, message: str) -> None:
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        print(f"[{stamp}] {message}", file=sys.stderr, flush=True)

    def command(
        self, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            args,
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        detail = result.stderr.strip() or result.stdout.strip()
        if result.returncode and is_network_error(detail):
            raise NetworkAccessRequired(
                "Gitea network access failed; rerun the same command with sandbox "
                f"network escalation: {detail}"
            )
        if check and result.returncode:
            raise MergeError(f"command failed ({' '.join(args)}): {detail}")
        return result

    def api_for_login(self, endpoint: str, login: str) -> Any:
        result = self.command(["tea", "api", "--login", login, endpoint], check=False)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise MergeError(f"tea api failed for login {login!r}: {detail}")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MergeError(f"Gitea API returned invalid JSON for {endpoint}") from exc
        message = str(value.get("message") or "") if isinstance(value, dict) else ""
        if message and is_network_error(message):
            raise NetworkAccessRequired(
                "Gitea network access failed; rerun the same command with sandbox "
                f"network escalation: {message}"
            )
        return value

    def api(self, endpoint: str) -> Any:
        if not self.login or not self.owner or not self.repo_name:
            raise MergeError("Gitea repository context is not initialized")
        resolved = endpoint.replace("{owner}", self.owner).replace(
            "{repo}", self.repo_name
        )
        value = self.api_for_login(resolved, self.login)
        if isinstance(value, dict) and value.get("message"):
            raise MergeError(f"Gitea API error for {resolved}: {value['message']}")
        return value

    def resolve_repository(self) -> None:
        origin = self.command(["git", "remote", "get-url", "origin"]).stdout.strip()
        host, owner, repo_name = parse_origin(origin)
        self.owner = owner
        self.repo_name = repo_name
        self.repo_slug = f"{owner}/{repo_name}"
        endpoint = f"/repos/{self.repo_slug}"

        raw = self.command(["tea", "login", "list", "--output", "json"]).stdout
        try:
            profiles = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MergeError("tea login list returned invalid JSON") from exc
        if self.requested_login:
            selected = next(
                (
                    profile
                    for profile in profiles
                    if profile.get("name") == self.requested_login
                ),
                None,
            )
            if selected is None:
                raise MergeError(
                    f"Tea login {self.requested_login!r} is not configured"
                )
            if not login_matches_host(selected, host):
                raise MergeError(
                    f"Tea login {self.requested_login!r} does not match origin host {host}"
                )
            candidates = [self.requested_login]
        else:
            candidates = [
                str(profile.get("name"))
                for profile in profiles
                if profile.get("name") and login_matches_host(profile, host)
            ]

        accessible = []
        errors: dict[str, str] = {}
        for login in candidates:
            try:
                repository = self.api_for_login(endpoint, login)
            except NetworkAccessRequired:
                raise
            except MergeError as exc:
                errors[login] = str(exc)
                continue
            if not isinstance(repository, dict):
                errors[login] = "Gitea returned unexpected repository data"
                continue
            if repository.get("full_name") != self.repo_slug:
                errors[login] = str(repository.get("message") or "repository not found")
                continue
            permissions = repository.get("permissions") or {}
            if not (permissions.get("push") or permissions.get("admin")):
                errors[login] = (
                    "repository is visible but the login has no push permission"
                )
                continue
            accessible.append((login, repository))

        if len(accessible) > 1 and not self.requested_login:
            names = ", ".join(login for login, _ in accessible)
            raise DecisionRequired(
                f"multiple Tea logins can merge {self.repo_slug}: {names}; pass --login"
            )
        if not accessible:
            detail = "; ".join(f"{name}: {error}" for name, error in errors.items())
            raise MergeError(
                f"no Tea login with push access to {self.repo_slug}; pass --login. {detail}"
            )

        self.login, repository = accessible[0]
        self.summary["login"] = self.login
        self.summary["repository"] = repository["full_name"]

    def stage(self, name: str):
        merger = self

        class Stage:
            def __enter__(self):
                self.started = time.monotonic()
                merger.log(name)
                return self

            def __exit__(self, exc_type, exc, traceback):
                merger.summary["stages"].append(
                    {
                        "name": name,
                        "duration_seconds": round(time.monotonic() - self.started, 2),
                        "status": "failed" if exc else "success",
                    }
                )
                return False

        return Stage()

    def validate_environment(self) -> None:
        for executable in ("git", "tea"):
            if not shutil.which(executable):
                raise MergeError(f"required executable not found: {executable}")
        root = self.command(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
        if Path(root).resolve() != self.repo:
            raise MergeError(f"--repo must be the Git worktree root: {root}")
        self.resolve_repository()

    def get_pr(self, number: int) -> dict[str, Any]:
        return self.api(f"/repos/{{owner}}/{{repo}}/pulls/{number}")

    def latest_review_states(self, number: int) -> dict[int, str]:
        reviews = self.api(f"/repos/{{owner}}/{{repo}}/pulls/{number}/reviews")
        latest: dict[int, tuple[int, str]] = {}
        for review in reviews:
            user_id = (review.get("user") or {}).get("id")
            if user_id is None:
                continue
            review_id = int(review.get("id") or 0)
            state = str(review.get("state") or "").upper()
            if user_id not in latest or review_id > latest[user_id][0]:
                latest[user_id] = (review_id, state)
        return {user_id: state for user_id, (_, state) in latest.items()}

    def pr_commits(self, number: int) -> list[dict[str, Any]]:
        if number not in self._commit_cache:
            self._commit_cache[number] = self.api(
                f"/repos/{{owner}}/{{repo}}/pulls/{number}/commits"
            )
        return self._commit_cache[number]

    def select_merge_strategy(self, number: int) -> dict[str, Any]:
        requested = self.requested_merge_strategy
        if requested != "auto":
            return {
                "requested": requested,
                "selected": requested,
                "reason": "explicitly selected by the user",
            }

        commits = self.pr_commits(number)
        titles = [
            str(commit.get("commit", {}).get("message") or "").splitlines()[0].strip()
            for commit in commits
        ]
        evidence: dict[str, Any] = {
            "requested": "auto",
            "commit_count": len(titles),
            "titles": titles,
        }
        if len(titles) == 1:
            return {
                **evidence,
                "selected": "rebase",
                "reason": "the PR contains one commit",
            }

        temporary = [title for title in titles if TEMPORARY_TITLE.search(title)]
        if temporary:
            return {
                **evidence,
                "selected": "squash",
                "reason": "temporary, CI, debug, or review-only commits were detected",
                "temporary_commits": temporary,
            }

        conventional_matches = [CONVENTIONAL_TITLE.match(title) for title in titles]
        conventional = all(conventional_matches)
        issue_sets = [set(ISSUE_REFERENCE.findall(title)) for title in titles]
        all_issues = set().union(*issue_sets) if issue_sets else set()
        if conventional and all(issue_sets) and len(all_issues) > 1:
            return {
                **evidence,
                "selected": "rebase",
                "reason": "clean conventional commits independently reference multiple Issues",
                "issues": sorted(all_issues),
            }
        if conventional and all(issue_sets) and len(all_issues) == 1:
            return {
                **evidence,
                "selected": "squash",
                "reason": "multiple conventional commits belong to one Issue",
                "issues": sorted(all_issues),
            }
        conventional_areas = {
            match.group("scope") or match.group("type")
            for match in conventional_matches
            if match
        }
        if conventional and not all_issues and len(conventional_areas) > 1:
            return {
                **evidence,
                "selected": "rebase",
                "reason": "clean conventional commits represent distinct scopes",
                "scopes": sorted(conventional_areas),
            }

        evidence.update(
            {
                "selected": None,
                "recommended": "squash",
                "alternatives": ["rebase", "merge"],
                "reason": "commit history does not provide enough evidence for a safe automatic choice",
            }
        )
        self.summary["merge_strategy"] = evidence
        raise DecisionRequired(evidence["reason"])

    def validate_pr(self, pr: dict[str, Any]) -> None:
        number = int(pr["number"])
        if (pr.get("base") or {}).get("ref") != self.base:
            raise MergeError(f"PR #{number} does not target {self.base}")
        if pr.get("merged"):
            return
        if pr.get("state") != "open":
            raise MergeError(f"PR #{number} is not open")
        if pr.get("mergeable") is False:
            raise MergeError(f"PR #{number} has conflicts")
        head_sha = (pr.get("head") or {}).get("sha")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            status_future = (
                pool.submit(
                    self.api,
                    f"/repos/{{owner}}/{{repo}}/commits/{head_sha}/status",
                )
                if not self.skip_ci_check
                else None
            )
            reviews_future = pool.submit(self.latest_review_states, number)
            commits_future = (
                pool.submit(
                    self.api,
                    f"/repos/{{owner}}/{{repo}}/pulls/{number}/commits",
                )
                if self.requested_merge_strategy == "auto"
                else None
            )
            status = status_future.result() if status_future else None
            review_states = reviews_future.result()
            if commits_future:
                self._commit_cache[number] = commits_future.result()

        if status is not None and str(status.get("state") or "").lower() != "success":
            raise MergeError(
                f"PR #{number} CI is {status.get('state') or 'missing'}, expected success"
            )
        blocked = [
            user_id
            for user_id, state in review_states.items()
            if state == "REQUEST_CHANGES"
        ]
        if blocked:
            raise MergeError(f"PR #{number} has outstanding request-changes reviews")

    def merge_pr(
        self,
        number: int,
        strategy: str,
        expected_head: str,
        delete_branch: bool,
    ) -> dict[str, Any]:
        endpoint = f"/repos/{self.repo_slug}/pulls/{number}/merge"
        command = [
            "tea",
            "api",
            "--login",
            str(self.login),
            "--method",
            "POST",
            "--field",
            f"do={strategy}",
            "--field",
            f"head_commit_id={expected_head}",
            "--Field",
            f"delete_branch_after_merge={str(delete_branch).lower()}",
            endpoint,
        ]
        try:
            result = self.command(command, check=False)
        except NetworkAccessRequired as exc:
            try:
                refreshed = self.get_pr(number)
            except MergeError:
                raise exc
            if refreshed.get("merged"):
                return refreshed
            raise exc
        refreshed = self.get_pr(number)
        if refreshed.get("merged"):
            return refreshed
        actual_head = (refreshed.get("head") or {}).get("sha")
        if actual_head != expected_head:
            raise MergeError(
                f"PR #{number} head changed: expected={expected_head}, actual={actual_head}"
            )
        detail = (
            result.stderr.strip() or result.stdout.strip() or "merge state unchanged"
        )
        raise MergeError(f"PR #{number} merge failed: {detail}")

    def base_head(self) -> str:
        branch = self.api(f"/repos/{{owner}}/{{repo}}/branches/{self.base}")
        commit = branch.get("commit") or {}
        return commit.get("id") or commit.get("sha") or ""

    def cleanup(
        self,
        feature_branch: str | None,
        expected_head: str | None,
        remote_cleanup_requested: bool,
    ) -> None:
        outcome: dict[str, Any] = {
            "requested": True,
            "local": "not-needed",
            "remote": (
                "requested atomically with merge"
                if remote_cleanup_requested
                else "skipped: PR was already merged"
            ),
        }
        protected = {self.base, "main", "master", "dev", "develop"}
        if not feature_branch or feature_branch in protected or not expected_head:
            self.summary["cleanup"] = outcome
            return
        if feature_branch.startswith(RELEASE_BRANCH_PREFIX):
            raise MergeError("refusing to clean a Release Please branch")

        current = self.command(["git", "branch", "--show-current"]).stdout.strip()
        if current == feature_branch:
            dirty = bool(self.command(["git", "status", "--porcelain"]).stdout.strip())
            if dirty:
                outcome["local"] = "skipped: feature branch is checked out and dirty"
                self.summary["cleanup"] = outcome
                return
            switched = self.command(["git", "switch", self.base], check=False)
            if switched.returncode:
                outcome["local"] = "skipped: could not switch to base branch"
                self.summary["cleanup"] = outcome
                return

        ref = f"refs/heads/{feature_branch}"
        local_exists = (
            self.command(
                ["git", "show-ref", "--verify", "--quiet", ref],
                check=False,
            ).returncode
            == 0
        )
        if local_exists:
            local_head = self.command(["git", "rev-parse", ref]).stdout.strip()
            if local_head != expected_head:
                outcome["local"] = "skipped: local feature branch moved"
            else:
                self.command(["git", "update-ref", "-d", ref, expected_head])
                outcome["local"] = f"deleted {feature_branch} at expected head"

        self.summary["cleanup"] = outcome


def require_merge_commit_sha(pr: dict[str, Any], number: int) -> str:
    value = str(pr.get("merge_commit_sha") or "")
    if not value:
        raise MergeError(
            f"PR #{number} is merged but Gitea did not return merge_commit_sha"
        )
    return value


def is_network_error(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in NETWORK_ERRORS)


def parse_origin(value: str) -> tuple[str, str, str]:
    if "://" in value:
        parsed = urlparse(value)
        host = parsed.hostname
        path = parsed.path
    else:
        match = re.fullmatch(r"(?:[^@]+@)?([^:]+):(.+)", value)
        if not match:
            raise MergeError(f"unsupported origin URL: {value}")
        host, path = match.groups()
    parts = [part for part in path.strip("/").removesuffix(".git").split("/") if part]
    if not host or len(parts) != 2:
        raise MergeError(f"origin must identify one Gitea owner/repository: {value}")
    return host.lower(), parts[0], parts[1]


def login_matches_host(profile: dict[str, Any], host: str) -> bool:
    url_host = urlparse(str(profile.get("url") or "")).hostname
    ssh_host = str(profile.get("ssh_host") or "").lower()
    return host == (url_host or "").lower() or host == ssh_host


def validate_repo_root(repo: Path) -> None:
    if not repo.is_dir():
        raise MergeError("--repo must be a local Git worktree root, not a slug or URL")
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode or Path(result.stdout.strip()).resolve() != repo:
        raise MergeError("--repo must be the root of a Git clone or linked worktree")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="Gitea worktree root")
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    parser.add_argument(
        "--merge-strategy",
        choices=("auto", "rebase", "squash", "merge"),
        default="auto",
    )
    parser.add_argument("--base", default="main", help="target branch")
    parser.add_argument("--login", help="Tea login profile; auto-detected by default")
    parser.add_argument("--cleanup", action="store_true", help="clean feature branch")
    parser.add_argument(
        "--skip-ci-check",
        action="store_true",
        help="allow merge without successful CI",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.expanduser().resolve()
    summary: dict[str, Any] | None = None
    try:
        validate_repo_root(repo)
        merger = Merger(
            repo=repo,
            base=args.base,
            merge_strategy=args.merge_strategy,
            dry_run=args.dry_run,
            skip_ci_check=args.skip_ci_check,
            login=args.login,
        )
        summary = merger.summary

        with merger.stage("validate environment"):
            merger.validate_environment()

        with merger.stage(f"validate PR #{args.pr}"):
            pr = merger.get_pr(args.pr)
            merger.validate_pr(pr)
            feature_branch = (pr.get("head") or {}).get("ref")
            feature_head = (pr.get("head") or {}).get("sha")
            if not feature_head:
                raise MergeError(f"PR #{args.pr} has no head SHA")
            summary["pr"] = {
                "number": args.pr,
                "url": pr.get("html_url"),
                "branch": feature_branch,
                "already_merged": bool(pr.get("merged")),
                "expected_head": feature_head,
            }
            strategy = (
                None if pr.get("merged") else merger.select_merge_strategy(args.pr)
            )
            summary["merge_strategy"] = strategy

        if args.dry_run:
            if strategy:
                merger.log(f"dry-run: would {strategy['selected']}-merge PR #{args.pr}")
            summary["status"] = "dry-run"
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        if pr.get("merged"):
            merger.log(f"PR #{args.pr} is already merged")
            merged = pr
        else:
            with merger.stage(f"{strategy['selected']} merge PR #{args.pr}"):
                merged = merger.merge_pr(
                    args.pr,
                    strategy["selected"],
                    feature_head,
                    args.cleanup,
                )

        summary["merge_commit_sha"] = require_merge_commit_sha(merged, args.pr)
        if args.cleanup:
            with merger.stage("synchronize and clean branch"):
                merger.cleanup(
                    feature_branch,
                    feature_head,
                    remote_cleanup_requested=not bool(pr.get("merged")),
                )
        else:
            summary["cleanup"] = {"requested": False}

        summary["status"] = "success"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (MergeError, OSError) as exc:
        if summary is None:
            summary = {"status": "failed", "repository": None, "stages": []}
        if isinstance(exc, NetworkAccessRequired):
            summary["status"] = "network_access_required"
        elif isinstance(exc, DecisionRequired):
            summary["status"] = "decision_required"
        else:
            summary["status"] = "failed"
        summary["error"] = str(exc)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
