#!/usr/bin/env python3
"""Deterministic Release Please publication for Gitea repositories."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

RELEASE_TITLE = re.compile(
    r"^chore\((?P<branch>[^)]+)\): release "
    r"(?:(?P<component>\S+)\s+)?"
    r"v?(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$"
)
PAGE_SIZE = 50
MAX_API_PAGES = 20
DECISIVE_REVIEW_STATES = {"APPROVED", "REQUEST_CHANGES"}
UNSUPPORTED_GLOB_CHARS = set("{}")
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
HTTP_STATUS = re.compile(r"^HTTP/\S+\s+(?P<status>\d{3})\b", re.MULTILINE)
FORCE_MERGE_GATE_MESSAGES = ("Not all required status checks successful",)


class ReleaseError(RuntimeError):
    pass


class NetworkAccessRequired(ReleaseError):
    pass


def api_response_status(result: subprocess.CompletedProcess[str]) -> int | None:
    for stream in (result.stderr, result.stdout):
        matches = list(HTTP_STATUS.finditer(stream))
        if matches:
            return int(matches[-1].group("status"))
    return None


def api_response_body(result: subprocess.CompletedProcess[str]) -> str:
    body = result.stdout.strip()
    normalized = body.replace("\r\n", "\n")
    matches = list(HTTP_STATUS.finditer(normalized))
    if matches:
        header_end = normalized.find("\n\n", matches[-1].end())
        if header_end >= 0:
            return normalized[header_end + 2 :].strip()
    return body


def api_response_message(result: subprocess.CompletedProcess[str]) -> str:
    body = api_response_body(result)
    if body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"]).strip()
        return body
    return ""


def format_api_failure(result: subprocess.CompletedProcess[str]) -> str:
    status = api_response_status(result)
    message = api_response_message(result)
    if status is not None and message:
        return f"HTTP {status}: {message}"
    return message or result.stderr.strip()


def is_force_merge_gate_failure(
    result: subprocess.CompletedProcess[str],
) -> bool:
    if api_response_status(result) != 405:
        return False
    message = api_response_message(result).casefold()
    return any(
        message == expected.casefold() or message.startswith(f"{expected.casefold()}:")
        for expected in FORCE_MERGE_GATE_MESSAGES
    )


def glob_matches_path(path: str, pattern: str) -> bool:
    cleaned = pattern.strip()
    if not cleaned:
        return False
    if any(char in cleaned for char in UNSUPPORTED_GLOB_CHARS):
        raise ReleaseError(
            f"unsupported branch-protection glob pattern {cleaned!r}; "
            "cannot safely verify protected files"
        )
    normalized_path = path.lstrip("/")
    normalized_pattern = cleaned.lstrip("/")
    if fnmatch.fnmatchcase(normalized_path, normalized_pattern):
        return True
    if "**/" in normalized_pattern:
        collapsed = normalized_pattern.replace("**/", "")
        if fnmatch.fnmatchcase(normalized_path, collapsed):
            return True
    return False


def matches_protected_file_patterns(
    paths: list[str],
    protected_patterns_raw: str,
    unprotected_patterns_raw: str,
) -> list[str]:
    protected_patterns = [
        item.strip() for item in protected_patterns_raw.split(";") if item.strip()
    ]
    if not protected_patterns:
        return []
    unprotected_patterns = [
        item.strip() for item in unprotected_patterns_raw.split(";") if item.strip()
    ]
    blocked: list[str] = []
    for path in paths:
        if any(glob_matches_path(path, pat) for pat in protected_patterns) and not any(
            glob_matches_path(path, pat) for pat in unprotected_patterns
        ):
            blocked.append(path)
    return blocked


def latest_decisive_review_states(reviews: list[dict[str, Any]]) -> dict[int, str]:
    latest: dict[int, tuple[int, str]] = {}
    for review in reviews:
        if review.get("dismissed"):
            continue
        state = str(review.get("state") or "").upper()
        if state not in DECISIVE_REVIEW_STATES:
            continue
        user_id = (review.get("user") or {}).get("id")
        if user_id is None:
            continue
        review_id = int(review.get("id") or 0)
        if user_id not in latest or review_id > latest[user_id][0]:
            latest[user_id] = (review_id, state)
    return {user_id: state for user_id, (_, state) in latest.items()}


def has_active_official_review_request(reviews: list[dict[str, Any]]) -> bool:
    for review in reviews:
        if review.get("dismissed"):
            continue
        if str(review.get("state") or "").upper() == "REQUEST_REVIEW":
            return True
    return False


def detect_include_v_in_tag(repo: Path) -> bool | None:
    config_path = repo / "release-please-config.json"
    if not config_path.is_file():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    packages = config.get("packages")
    if isinstance(packages, dict):
        root_pkg = packages.get(".")
        if isinstance(root_pkg, dict) and isinstance(
            root_pkg.get("include-v-in-tag"), bool
        ):
            return root_pkg["include-v-in-tag"]
    if isinstance(config.get("include-v-in-tag"), bool):
        return config["include-v-in-tag"]
    return None


class Publisher:
    def __init__(
        self,
        repo: Path,
        base: str,
        workflow: str,
        release_head_prefix: str,
        poll_interval: int,
        timeout: int,
        dry_run: bool,
        login: str | None,
        tag_prefix: str | None = None,
    ) -> None:
        self.repo = repo
        self.base = base
        self.workflow = workflow
        self.release_head_prefix = release_head_prefix
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.dry_run = dry_run
        self.requested_login = login
        self.tag_prefix = tag_prefix
        self.login: str | None = None
        self.can_force_merge = False
        self.owner: str | None = None
        self.repo_name: str | None = None
        self.repo_slug: str | None = None
        self.summary: dict[str, Any] = {
            "repository": None,
            "login": None,
            "base": base,
            "workflow": workflow,
            "status": "running",
            "dry_run": dry_run,
            "release_pr": None,
            "version": None,
            "tag": None,
            "workflow_run": None,
            "force_merge_used": False,
            "warnings": [],
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
            raise ReleaseError(f"command failed ({' '.join(args)}): {detail}")
        return result

    def api_for_login(self, endpoint: str, login: str) -> Any:
        result = self.command(["tea", "api", "--login", login, endpoint], check=False)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ReleaseError(f"tea api failed for login {login!r}: {detail}")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError(
                f"Gitea API returned invalid JSON for {endpoint}"
            ) from exc
        message = str(value.get("message") or "") if isinstance(value, dict) else ""
        if message and is_network_error(message):
            raise NetworkAccessRequired(
                "Gitea network access failed; rerun the same command with sandbox "
                f"network escalation: {message}"
            )
        return value

    def api(self, endpoint: str) -> Any:
        if not self.login or not self.owner or not self.repo_name:
            raise ReleaseError("Gitea repository context is not initialized")
        resolved = endpoint.replace("{owner}", self.owner).replace(
            "{repo}", self.repo_name
        )
        value = self.api_for_login(resolved, self.login)
        if isinstance(value, dict) and value.get("message"):
            raise ReleaseError(f"Gitea API error for {resolved}: {value['message']}")
        return value

    def api_paginated(
        self,
        endpoint: str,
        *,
        page_size: int = PAGE_SIZE,
        max_pages: int = MAX_API_PAGES,
    ) -> list[Any]:
        separator = "&" if "?" in endpoint else "?"
        items: list[Any] = []
        for page in range(1, max_pages + 1):
            batch = self.api(f"{endpoint}{separator}page={page}&limit={page_size}")
            if not isinstance(batch, list):
                raise ReleaseError(
                    f"Gitea API returned unexpected list data for {endpoint}"
                )
            items.extend(batch)
            if len(batch) < page_size:
                return items
        raise ReleaseError(
            f"Gitea API pagination exceeded {page_size * max_pages} items for {endpoint}"
        )

    def resolve_repository(self) -> None:
        origin = self.command(["git", "remote", "get-url", "origin"]).stdout.strip()
        host, http_port, owner, repo_name = parse_origin(origin)
        self.owner = owner
        self.repo_name = repo_name
        self.repo_slug = f"{owner}/{repo_name}"
        endpoint = f"/repos/{self.repo_slug}"

        raw = self.command(["tea", "login", "list", "--output", "json"]).stdout
        try:
            profiles = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReleaseError("tea login list returned invalid JSON") from exc
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
                raise ReleaseError(
                    f"Tea login {self.requested_login!r} is not configured"
                )
            if not login_matches_host(selected, host, http_port):
                raise ReleaseError(
                    f"Tea login {self.requested_login!r} does not match origin host {host}"
                )
            candidates = [self.requested_login]
        else:
            candidates = [
                str(profile.get("name"))
                for profile in profiles
                if profile.get("name") and login_matches_host(profile, host, http_port)
            ]

        accessible = []
        errors: dict[str, str] = {}
        network_error: NetworkAccessRequired | None = None
        for login in candidates:
            try:
                repository = self.api_for_login(endpoint, login)
            except NetworkAccessRequired as exc:
                network_error = network_error or exc
                errors[login] = str(exc)
                continue
            except ReleaseError as exc:
                errors[login] = str(exc)
                continue
            if not isinstance(repository, dict):
                errors[login] = "Gitea returned unexpected repository data"
                continue
            full_name = str(repository.get("full_name") or "")
            if full_name.casefold() != self.repo_slug.casefold():
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
            raise ReleaseError(
                f"multiple Tea logins can publish {self.repo_slug}: {names}; pass --login"
            )
        if not accessible:
            if network_error is not None:
                raise network_error
            detail = "; ".join(f"{name}: {error}" for name, error in errors.items())
            raise ReleaseError(
                f"no Tea login with push access to {self.repo_slug}; pass --login. {detail}"
            )

        self.login, repository = accessible[0]
        canonical_full_name = str(repository["full_name"])
        if "/" in canonical_full_name:
            self.owner, self.repo_name = canonical_full_name.split("/", 1)
            self.repo_slug = canonical_full_name
        self.can_force_merge = bool((repository.get("permissions") or {}).get("admin"))
        self.summary["login"] = self.login
        self.summary["repository"] = canonical_full_name

    def stage(self, name: str):
        publisher = self

        class Stage:
            def __enter__(self):
                self.started = time.monotonic()
                publisher.log(name)
                return self

            def __exit__(self, exc_type, exc, traceback):
                publisher.summary["stages"].append(
                    {
                        "name": name,
                        "duration_seconds": round(time.monotonic() - self.started, 2),
                        "status": "failed" if exc else "success",
                    }
                )
                return False

        return Stage()

    def wait(self, description: str, getter):
        deadline = time.monotonic() + self.timeout
        while True:
            value = getter()
            if value is not None:
                return value
            if time.monotonic() >= deadline:
                raise ReleaseError(
                    f"timed out waiting for {description} after {self.timeout}s"
                )
            time.sleep(self.poll_interval)

    def validate_environment(self) -> None:
        for executable in ("git", "tea"):
            if not shutil.which(executable):
                raise ReleaseError(f"required executable not found: {executable}")
        root = self.command(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
        if Path(root).resolve() != self.repo:
            raise ReleaseError(f"--repo must be the Git worktree root: {root}")
        self.resolve_repository()

    def get_pr(self, number: int) -> dict[str, Any]:
        return self.api(f"/repos/{{owner}}/{{repo}}/pulls/{number}")

    def open_release_prs(self) -> list[dict[str, Any]]:
        pulls = self.api_paginated("/repos/{owner}/{repo}/pulls?state=open")
        matches = []
        for pr in pulls:
            head = (pr.get("head") or {}).get("ref", "")
            base = (pr.get("base") or {}).get("ref")
            if base == self.base and head.startswith(self.release_head_prefix):
                matches.append(pr)
        return sorted(matches, key=lambda item: item["number"])

    def select_release_pr(self, number: int | None) -> dict[str, Any]:
        if number is not None:
            return self.get_pr(number)
        candidates = self.open_release_prs()
        if len(candidates) != 1:
            raise ReleaseError(
                f"expected exactly one open Release PR, found {len(candidates)}"
            )
        return candidates[0]

    def validate_release_pr(self, pr: dict[str, Any]) -> str:
        number = int(pr["number"])
        if pr.get("state") != "open" and not pr.get("merged"):
            raise ReleaseError(f"Release PR #{number} is not open")
        if pr.get("draft") and not pr.get("merged"):
            raise ReleaseError(f"Release PR #{number} is a draft")
        title = str(pr.get("title") or "")
        match = RELEASE_TITLE.fullmatch(title)
        if not match:
            raise ReleaseError(f"unexpected Release PR title: {title!r}")
        if match.group("branch") != self.base:
            raise ReleaseError(
                f"Release PR title targets {match.group('branch')}, not {self.base}"
            )
        if (pr.get("base") or {}).get("ref") != self.base:
            raise ReleaseError(f"Release PR does not target {self.base}")
        head = (pr.get("head") or {}).get("ref", "")
        if not head.startswith(self.release_head_prefix):
            raise ReleaseError("PR is not a Release Please branch")
        if pr.get("mergeable") is False and not pr.get("merged"):
            raise ReleaseError("Release PR has conflicts")
        if pr.get("mergeable") is not True and not pr.get("merged"):
            raise ReleaseError("Release PR conflict status is not confirmed")
        return match.group("version")

    def branch_protection_for_base(self) -> dict[str, Any]:
        branch = quote(self.base, safe="")
        try:
            protection = self.api(
                f"/repos/{{owner}}/{{repo}}/branch_protections/{branch}"
            )
            if isinstance(protection, dict):
                return protection
        except NetworkAccessRequired:
            raise
        except ReleaseError:
            pass

        rules = self.api("/repos/{owner}/{repo}/branch_protections")
        if not isinstance(rules, list):
            raise ReleaseError("Gitea returned invalid branch protection data")
        matched: list[tuple[int, int, dict[str, Any]]] = []
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            pattern = str(rule.get("rule_name") or rule.get("branch_name") or "")
            if pattern and glob_matches_path(self.base, pattern):
                priority = int(rule.get("priority") or 0)
                matched.append((priority, index, rule))
        if not matched:
            raise ReleaseError(
                f"no matching branch protection rule found for {self.base}"
            )
        matched.sort(key=lambda item: (item[0], item[1]))
        return matched[0][2]

    def pr_reviews(self, number: int) -> list[dict[str, Any]]:
        return self.api_paginated(f"/repos/{{owner}}/{{repo}}/pulls/{number}/reviews")

    def pr_files(self, number: int) -> list[dict[str, Any]]:
        return self.api_paginated(f"/repos/{{owner}}/{{repo}}/pulls/{number}/files")

    def pr_commits(self, number: int) -> list[dict[str, Any]]:
        return self.api_paginated(f"/repos/{{owner}}/{{repo}}/pulls/{number}/commits")

    def validate_release_force_merge_policy(
        self, expected_head: str, pr: dict[str, Any] | None = None
    ) -> None:
        protection = self.branch_protection_for_base()
        if not isinstance(protection, dict):
            raise ReleaseError("Gitea returned invalid branch protection data")
        required_approvals = int(protection.get("required_approvals") or 0)
        requires_ci = bool(protection.get("enable_status_check"))
        requires_review = required_approvals > 0
        if not (requires_ci and requires_review):
            raise ReleaseError(
                "administrator force merge requires target branch protection to "
                "enable both CI status checks and required approvals"
            )

        head = quote(expected_head, safe="")
        combined_status = self.api(
            f"/repos/{{owner}}/{{repo}}/commits/{head}/status?limit=1"
        )
        if (
            not isinstance(combined_status, dict)
            or "total_count" not in combined_status
        ):
            raise ReleaseError("Gitea returned invalid commit status data")
        statuses = combined_status.get("statuses")
        if statuses is not None and not isinstance(statuses, list):
            raise ReleaseError("Gitea returned invalid commit status data")
        if int(combined_status["total_count"]) != 0 or statuses:
            raise ReleaseError(
                "administrator force merge is limited to Release PR heads with no "
                "CI status results"
            )

        if pr is None:
            return

        number = int(pr["number"])
        if pr.get("draft"):
            raise ReleaseError(
                "administrator force merge refused because the Release PR is a draft"
            )

        reviews = self.pr_reviews(number)
        decisive_states = latest_decisive_review_states(reviews)
        if any(state == "REQUEST_CHANGES" for state in decisive_states.values()):
            raise ReleaseError(
                "administrator force merge refused because the Release PR has "
                "outstanding request-changes reviews"
            )
        if protection.get(
            "block_on_official_review_requests"
        ) and has_active_official_review_request(reviews):
            raise ReleaseError(
                "administrator force merge refused because the Release PR has "
                "pending official review requests"
            )

        if protection.get("block_on_outdated_branch"):
            branch_data = self.api(
                f"/repos/{{owner}}/{{repo}}/branches/{quote(self.base, safe='')}"
            )
            if not isinstance(branch_data, dict):
                raise ReleaseError("Gitea returned invalid base branch data")
            base_commit = branch_data.get("commit") or {}
            base_sha = str(base_commit.get("id") or base_commit.get("sha") or "")
            if not base_sha:
                raise ReleaseError(
                    "could not verify base branch head for outdated-branch check"
                )
            comparison = self.api(
                f"/repos/{{owner}}/{{repo}}/compare/"
                f"{head}...{quote(base_sha, safe='')}"
            )
            if not isinstance(comparison, dict) or "total_commits" not in comparison:
                raise ReleaseError(
                    "could not verify whether the Release PR branch is outdated"
                )
            if int(comparison["total_commits"]) != 0:
                raise ReleaseError(
                    "administrator force merge refused because the Release PR branch "
                    f"is behind {self.base}"
                )

        protected_patterns = str(protection.get("protected_file_patterns") or "")
        if protected_patterns.strip():
            files = self.pr_files(number)
            changed_paths: list[str] = []
            for item in files:
                if not isinstance(item, dict):
                    continue
                for key in ("filename", "previous_filename"):
                    path = str(item.get(key) or "").strip()
                    if path:
                        changed_paths.append(path)
            blocked_files = matches_protected_file_patterns(
                changed_paths,
                protected_patterns,
                str(protection.get("unprotected_file_patterns") or ""),
            )
            if blocked_files:
                raise ReleaseError(
                    "administrator force merge refused because the Release PR "
                    f"modifies protected files: {', '.join(blocked_files)}"
                )

        if protection.get("require_signed_commits"):
            commits = self.pr_commits(number)
            if not commits:
                raise ReleaseError(
                    "administrator force merge refused because commit signatures "
                    "could not be verified"
                )
            for commit in commits:
                verification = (commit.get("commit") or {}).get("verification") or {}
                if verification.get("verified") is not True:
                    raise ReleaseError(
                        "administrator force merge refused because the Release PR "
                        "contains unsigned or unverified commits"
                    )

    def merge_release_pr(self, number: int, expected_head: str) -> dict[str, Any]:
        pr = self.get_pr(number)
        if pr.get("merged"):
            merged_head = (pr.get("head") or {}).get("sha")
            if merged_head and merged_head != expected_head:
                raise ReleaseError(
                    f"Release PR #{number} was already merged at a different head: "
                    f"expected={expected_head}, actual={merged_head}"
                )
            self.log(f"Release PR #{number} is already merged")
            return pr
        endpoint = f"/repos/{self.repo_slug}/pulls/{number}/merge"

        def merge_command(*, force: bool) -> list[str]:
            command = [
                "tea",
                "api",
                "--login",
                str(self.login),
                "--include",
                "--method",
                "POST",
                "--field",
                "do=rebase",
                "--field",
                f"head_commit_id={expected_head}",
                "--Field",
                "delete_branch_after_merge=true",
            ]
            if force:
                command.extend(["--Field", "force_merge=true"])
            command.append(endpoint)
            return command

        try:
            result = self.command(merge_command(force=False), check=False)
        except NetworkAccessRequired as exc:
            try:
                refreshed = self.get_pr(number)
            except ReleaseError:
                raise exc
            if refreshed.get("merged"):
                return refreshed
            raise exc
        refreshed = self.get_pr(number)
        if refreshed.get("merged"):
            return refreshed
        actual_head = (refreshed.get("head") or {}).get("sha")
        if actual_head != expected_head:
            raise ReleaseError(
                f"Release PR #{number} head changed: "
                f"expected={expected_head}, actual={actual_head}"
            )
        normal_detail = format_api_failure(result) or "merge state unchanged"
        if not is_force_merge_gate_failure(result):
            raise ReleaseError(
                f"Release PR #{number} normal merge failed: {normal_detail}; "
                "administrator force merge refused because Gitea did not report "
                "a recognized branch-protection gate"
            )
        try:
            self.validate_release_pr(refreshed)
        except ReleaseError as exc:
            raise ReleaseError(
                f"Release PR #{number} normal merge failed: {normal_detail}; "
                f"administrator force merge refused: {exc}"
            ) from exc
        if refreshed.get("mergeable") is not True:
            raise ReleaseError(
                f"Release PR #{number} normal merge failed: {normal_detail}; "
                "administrator force merge refused because Gitea did not confirm "
                "that the PR is conflict-free"
            )
        if not self.can_force_merge:
            raise ReleaseError(
                f"Release PR #{number} normal merge failed: {normal_detail}; "
                f"Tea login {self.login!r} has no repository administrator permission"
            )
        try:
            self.validate_release_force_merge_policy(expected_head, pr=refreshed)
        except (ReleaseError, TypeError, ValueError) as exc:
            raise ReleaseError(
                f"Release PR #{number} normal merge failed: {normal_detail}; "
                f"administrator force merge refused: {exc}"
            ) from exc

        self.log(
            f"Release PR #{number} is blocked by the normal merge gate; "
            "trying one administrator force merge"
        )
        try:
            force_result = self.command(merge_command(force=True), check=False)
        except NetworkAccessRequired as exc:
            try:
                force_refreshed = self.get_pr(number)
            except ReleaseError:
                raise exc
            if force_refreshed.get("merged"):
                self.summary["force_merge_used"] = True
                return force_refreshed
            raise exc

        force_refreshed = self.get_pr(number)
        if force_refreshed.get("merged"):
            self.summary["force_merge_used"] = True
            return force_refreshed
        force_head = (force_refreshed.get("head") or {}).get("sha")
        if force_head != expected_head:
            raise ReleaseError(
                f"Release PR #{number} head changed during administrator force merge: "
                f"expected={expected_head}, actual={force_head}"
            )
        force_detail = format_api_failure(force_result) or "merge state unchanged"
        raise ReleaseError(
            f"Release PR #{number} normal merge failed: {normal_detail}; "
            f"administrator force merge failed: {force_detail}"
        )

    def workflow_runs(self) -> list[dict[str, Any]]:
        branch = quote(self.base, safe="")
        data = self.api(
            f"/repos/{{owner}}/{{repo}}/actions/runs?branch={branch}&limit=30"
        )
        return data.get("workflow_runs") or data.get("runs") or []

    def workflow_jobs(self, run_id: int) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for page in range(1, MAX_API_PAGES + 1):
            data = self.api(
                f"/repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs"
                f"?page={page}&limit={PAGE_SIZE}"
            )
            if not isinstance(data, dict):
                raise ReleaseError(
                    f"Gitea returned invalid jobs data for workflow #{run_id}"
                )
            batch = data.get("jobs") or []
            if not isinstance(batch, list):
                raise ReleaseError(
                    f"Gitea returned invalid jobs list for workflow #{run_id}"
                )
            jobs.extend(batch)
            total = data.get("total_count")
            if len(batch) < PAGE_SIZE or (
                isinstance(total, int) and len(jobs) >= total
            ):
                return jobs
        raise ReleaseError(
            f"workflow #{run_id} jobs exceeded {PAGE_SIZE * MAX_API_PAGES} items"
        )

    def wait_for_workflow(self, head_sha: str) -> dict[str, Any]:
        def find():
            candidates = []
            for run in self.workflow_runs():
                path = str(run.get("path") or "")
                if (
                    run.get("head_sha") == head_sha
                    and Path(path.split("@", 1)[0]).name == self.workflow
                ):
                    candidates.append(run)
            return (
                max(candidates, key=lambda item: item.get("id", 0))
                if candidates
                else None
            )

        return self.wait("release workflow creation", find)

    def wait_for_workflow_completion(self, run_id: int) -> dict[str, Any]:
        def completed():
            run = self.api(f"/repos/{{owner}}/{{repo}}/actions/runs/{run_id}")
            if str(run.get("status") or "").lower() in {"completed", "complete"}:
                return run
            return None

        run = self.wait(f"workflow #{run_id} completion", completed)
        if str(run.get("conclusion") or "").lower() != "success":
            raise ReleaseError(
                f"workflow #{run_id} concluded {run.get('conclusion') or 'without conclusion'}"
            )
        jobs = self.workflow_jobs(run_id)
        failed = [
            job.get("name")
            for job in jobs
            if str(job.get("conclusion") or "").lower() not in {"success", "skipped"}
        ]
        if failed:
            raise ReleaseError(f"workflow #{run_id} has unsuccessful jobs: {failed}")
        if not any(
            str(job.get("conclusion") or "").lower() == "success" for job in jobs
        ):
            raise ReleaseError(f"workflow #{run_id} has no successful jobs")
        return run

    def candidate_tags(self, version: str) -> list[str]:
        if self.tag_prefix is not None:
            return [f"{self.tag_prefix}{version}"]
        include_v = detect_include_v_in_tag(self.repo)
        if include_v is False:
            return [version, f"v{version}"]
        return [f"v{version}", version]

    def verify_tag(
        self, version: str, expected_commit: str | None = None
    ) -> str:
        candidates = self.candidate_tags(version)
        last_error = ""
        for tag_name in candidates:
            encoded = quote(tag_name, safe="")
            try:
                tag = self.api(f"/repos/{{owner}}/{{repo}}/tags/{encoded}")
            except NetworkAccessRequired:
                raise
            except ReleaseError as exc:
                last_error = str(exc)
                continue
            if not isinstance(tag, dict) or tag.get("name") != tag_name:
                last_error = f"Tag API returned unexpected data for {tag_name}"
                continue
            if expected_commit is not None:
                commit_sha = str((tag.get("commit") or {}).get("sha") or "")
                if not commit_sha:
                    raise ReleaseError(
                        f"remote Tag {tag_name} did not include a target commit SHA"
                    )
                if commit_sha != expected_commit:
                    raise ReleaseError(
                        f"remote Tag {tag_name} points to {commit_sha}, "
                        f"expected {expected_commit}"
                    )
            return tag_name
        detail = f": {last_error}" if last_error else ""
        raise ReleaseError(
            f"remote Tag for {version} ({', '.join(candidates)}) was not found{detail}"
        )

    def verify_release(self, tag_name: str) -> None:
        encoded = quote(tag_name, safe="")
        try:
            release = self.api(f"/repos/{{owner}}/{{repo}}/releases/tags/{encoded}")
            if release.get("tag_name") != tag_name:
                raise ReleaseError(
                    f"Release API returned the wrong tag for {tag_name}"
                )
        except NetworkAccessRequired:
            raise
        except ReleaseError as exc:
            warning = (
                f"Release API verification failed after workflow and Tag success: {exc}"
            )
            self.summary["warnings"].append(warning)
            self.log(f"warning: {warning}")


def is_network_error(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in NETWORK_ERRORS)


def effective_http_port(parsed) -> int | None:
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def parse_origin(value: str) -> tuple[str, int | None, str, str]:
    if "://" in value:
        parsed = urlparse(value)
        host = parsed.hostname
        http_port = effective_http_port(parsed)
        path = parsed.path
    else:
        match = re.fullmatch(r"(?:[^@]+@)?([^:]+):(.+)", value)
        if not match:
            raise ReleaseError(f"unsupported origin URL: {value}")
        host, path = match.groups()
        http_port = None
    parts = [part for part in path.strip("/").removesuffix(".git").split("/") if part]
    if not host or len(parts) != 2:
        raise ReleaseError(f"origin must identify one Gitea owner/repository: {value}")
    return host.lower(), http_port, parts[0], parts[1]


def login_matches_host(
    profile: dict[str, Any], host: str, http_port: int | None = None
) -> bool:
    parsed = urlparse(str(profile.get("url") or ""))
    url_host = (parsed.hostname or "").lower()
    ssh_host = str(profile.get("ssh_host") or "").lower()
    if http_port is not None:
        if host == url_host and effective_http_port(parsed) == http_port:
            return True
        return not url_host and host == ssh_host
    return host == url_host or host == ssh_host


def validate_repo_root(repo: Path) -> None:
    if not repo.is_dir():
        raise ReleaseError(
            "--repo must be a local Git worktree root, not a slug or URL"
        )
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode or Path(result.stdout.strip()).resolve() != repo:
        raise ReleaseError("--repo must be the root of a Git clone or linked worktree")


def detect_workflow(repo: Path, requested: str | None) -> str:
    if requested:
        return Path(requested).name
    for name in ("release.yaml", "release.yml"):
        if (repo / ".gitea" / "workflows" / name).is_file():
            return name
    raise ReleaseError("release workflow not found; pass --workflow <filename>")


def require_merge_commit_sha(pr: dict[str, Any], number: int) -> str:
    value = str(pr.get("merge_commit_sha") or "")
    if not value:
        raise ReleaseError(
            f"Release PR #{number} is merged but Gitea did not return merge_commit_sha"
        )
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="Gitea worktree root")
    parser.add_argument("--release-pr", type=int, help="specific Release Please PR")
    parser.add_argument("--base", default="main", help="release target branch")
    parser.add_argument("--login", help="Tea login profile; auto-detected by default")
    parser.add_argument("--workflow", help="release workflow filename")
    parser.add_argument("--release-head-prefix", help="Release Please branch prefix")
    parser.add_argument(
        "--tag-prefix",
        help="expected Git tag prefix (for example 'v' or ''); auto-detected by default",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--poll-interval", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if args.poll_interval < 1 or args.timeout < 1:
        parser.error("poll interval and timeout must be positive")
    return args


def main() -> int:
    args = parse_args()
    repo = args.repo.expanduser().resolve()
    summary: dict[str, Any] | None = None
    try:
        validate_repo_root(repo)
        workflow = detect_workflow(repo, args.workflow)
        publisher = Publisher(
            repo=repo,
            base=args.base,
            workflow=workflow,
            release_head_prefix=(
                args.release_head_prefix or f"release-please--branches--{args.base}"
            ),
            poll_interval=args.poll_interval,
            timeout=args.timeout,
            dry_run=args.dry_run,
            login=args.login,
            tag_prefix=args.tag_prefix,
        )
        summary = publisher.summary

        with publisher.stage("validate environment"):
            publisher.validate_environment()

        with publisher.stage("select Release PR"):
            release_pr = publisher.select_release_pr(args.release_pr)

        with publisher.stage("validate Release PR"):
            version = publisher.validate_release_pr(release_pr)
            release_number = int(release_pr["number"])
            release_expected_head = (release_pr.get("head") or {}).get("sha")
            if not release_expected_head:
                raise ReleaseError(f"Release PR #{release_number} has no head SHA")
            summary["version"] = version
            summary["release_pr"] = {
                "number": release_number,
                "url": release_pr.get("html_url"),
                "already_merged": bool(release_pr.get("merged")),
                "expected_head": release_expected_head,
            }

        if args.dry_run:
            publisher.log(
                f"dry-run: would publish {version} from Release PR #{release_number}"
            )
            summary["status"] = "dry-run"
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        with publisher.stage(f"rebase merge Release PR #{release_number}"):
            merged_release = publisher.merge_release_pr(
                release_number, release_expected_head
            )
            release_head = require_merge_commit_sha(merged_release, release_number)

        with publisher.stage("wait for release workflow"):
            run = publisher.wait_for_workflow(release_head)
            run_id = int(run["id"])
            summary["workflow_run"] = {
                "id": run_id,
                "url": run.get("html_url"),
            }

        with publisher.stage(f"wait for workflow #{run_id}"):
            completed_run = publisher.wait_for_workflow_completion(run_id)
            summary["workflow_run"]["url"] = (
                completed_run.get("html_url") or summary["workflow_run"]["url"]
            )

        with publisher.stage(f"verify release {version}"):
            tag_name = publisher.verify_tag(version, expected_commit=release_head)
            summary["tag"] = tag_name
            publisher.verify_release(tag_name)

        summary["status"] = "success"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (ReleaseError, OSError) as exc:
        if summary is None:
            summary = {"status": "failed", "repository": None, "stages": []}
        summary["status"] = (
            "network_access_required"
            if isinstance(exc, NetworkAccessRequired)
            else "failed"
        )
        summary["error"] = str(exc)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
