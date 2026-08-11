#!/usr/bin/env python3
"""Deterministic Release Please publication for Gitea repositories."""

from __future__ import annotations

import argparse
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

RELEASE_TITLE = re.compile(
    r"^chore\((?P<branch>[^)]+)\): release "
    r"(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$"
)
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
FORCE_MERGE_GATE_MESSAGES = (
    "Not all required status checks successful",
    "Does not have enough approvals",
    "There are requested changes",
    "There are official review requests",
    "The head branch is behind the base branch",
    "Changed protected files",
)


class ReleaseError(RuntimeError):
    pass


class NetworkAccessRequired(ReleaseError):
    pass


def api_response_status(result: subprocess.CompletedProcess[str]) -> int | None:
    matches = list(HTTP_STATUS.finditer(result.stderr))
    return int(matches[-1].group("status")) if matches else None


def api_response_message(result: subprocess.CompletedProcess[str]) -> str:
    body = result.stdout.strip()
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
    ) -> None:
        self.repo = repo
        self.base = base
        self.workflow = workflow
        self.release_head_prefix = release_head_prefix
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.dry_run = dry_run
        self.requested_login = login
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
            if not login_matches_host(selected, host):
                raise ReleaseError(
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
            except ReleaseError as exc:
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
            raise ReleaseError(
                f"multiple Tea logins can publish {self.repo_slug}: {names}; pass --login"
            )
        if not accessible:
            detail = "; ".join(f"{name}: {error}" for name, error in errors.items())
            raise ReleaseError(
                f"no Tea login with push access to {self.repo_slug}; pass --login. {detail}"
            )

        self.login, repository = accessible[0]
        self.can_force_merge = bool((repository.get("permissions") or {}).get("admin"))
        self.summary["login"] = self.login
        self.summary["repository"] = repository["full_name"]

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
        pulls = self.api("/repos/{owner}/{repo}/pulls?state=open&limit=50")
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
        return match.group("version")

    def merge_release_pr(self, number: int, expected_head: str) -> dict[str, Any]:
        pr = self.get_pr(number)
        if pr.get("merged"):
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
                command.extend(["--field", "force_merge=true"])
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
        data = self.api(
            f"/repos/{{owner}}/{{repo}}/actions/runs?branch={self.base}&limit=30"
        )
        return data.get("workflow_runs") or data.get("runs") or []

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
        jobs_data = self.api(f"/repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs")
        jobs = jobs_data.get("jobs") or []
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

    def verify_tag(self, version: str) -> None:
        result = self.command(
            [
                "git",
                "ls-remote",
                "--exit-code",
                "--tags",
                "origin",
                f"refs/tags/{version}",
            ],
            check=False,
        )
        if result.returncode:
            raise ReleaseError(f"remote Tag {version} was not found")

    def verify_release(self, version: str) -> None:
        try:
            release = self.api(f"/repos/{{owner}}/{{repo}}/releases/tags/{version}")
            if release.get("tag_name") != version:
                raise ReleaseError(f"Release API returned the wrong tag for {version}")
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


def parse_origin(value: str) -> tuple[str, str, str]:
    if "://" in value:
        parsed = urlparse(value)
        host = parsed.hostname
        path = parsed.path
    else:
        match = re.fullmatch(r"(?:[^@]+@)?([^:]+):(.+)", value)
        if not match:
            raise ReleaseError(f"unsupported origin URL: {value}")
        host, path = match.groups()
    parts = [part for part in path.strip("/").removesuffix(".git").split("/") if part]
    if not host or len(parts) != 2:
        raise ReleaseError(f"origin must identify one Gitea owner/repository: {value}")
    return host.lower(), parts[0], parts[1]


def login_matches_host(profile: dict[str, Any], host: str) -> bool:
    url_host = urlparse(str(profile.get("url") or "")).hostname
    ssh_host = str(profile.get("ssh_host") or "").lower()
    return host == (url_host or "").lower() or host == ssh_host


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
            publisher.verify_tag(version)
            publisher.verify_release(version)

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
