#!/usr/bin/env python3
"""Fast, API-only Gitea pull-request review helper.

The script deliberately separates deterministic work (fetching, caching,
filtering, head guards and review read-back) from the actual code judgement.
It shells out to ``tea`` so credentials remain in tea's login store.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

PAGE_SIZE = 50
MAX_FILE_PAGES = 8
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


@dataclass(frozen=True)
class PullRef:
    host: str
    owner: str
    repo: str
    index: int
    url: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_pull_url(value: str) -> PullRef:
    parsed = urlparse(value)
    match = re.fullmatch(r"/([^/]+)/([^/]+)/pulls/(\d+)/?", parsed.path)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not match:
        raise ValueError(f"不是有效的 Gitea PR 地址: {value}")
    owner, repo, index = match.groups()
    return PullRef(
        (parsed.hostname or parsed.netloc).lower(),
        owner,
        repo,
        int(index),
        value.rstrip("/"),
    )


def cache_root() -> Path:
    configured = os.environ.get("GITEA_REVIEW_CACHE")
    if configured:
        return Path(configured).expanduser()
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "gitea-review"


def safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def is_network_error(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in NETWORK_ERRORS)


def resolve_login(pull: PullRef, requested: str | None) -> str:
    process = subprocess.run(
        ["tea", "login", "list", "--output", "json"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or "tea login list failed")
    try:
        profiles = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("tea login list returned invalid JSON") from exc
    if requested:
        selected = next(
            (profile for profile in profiles if profile.get("name") == requested),
            None,
        )
        if selected is None:
            raise RuntimeError(f"Tea login {requested!r} is not configured")
        profile_hosts = {
            (urlparse(str(selected.get("url") or "")).hostname or "").lower(),
            str(selected.get("ssh_host") or "").lower(),
        }
        if pull.host not in profile_hosts:
            raise RuntimeError(
                f"Tea login {requested!r} does not match PR host {pull.host}"
            )
        candidates = [requested]
    else:
        candidates = [
            str(profile.get("name"))
            for profile in profiles
            if profile.get("name")
            and (
                pull.host
                == (urlparse(str(profile.get("url") or "")).hostname or "").lower()
                or pull.host == str(profile.get("ssh_host") or "").lower()
            )
        ]

    accessible = []
    errors: dict[str, str] = {}
    endpoint = f"/repos/{pull.slug}"
    for login in candidates:
        process = subprocess.run(
            ["tea", "api", "--login", login, endpoint],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        detail = process.stderr.strip() or process.stdout.strip()
        if process.returncode:
            if is_network_error(detail):
                raise RuntimeError(
                    "network_access_required: rerun the same command with sandbox "
                    f"network escalation: {detail}"
                )
            errors[login] = detail
            continue
        try:
            repository = json.loads(process.stdout)
        except json.JSONDecodeError:
            errors[login] = "Gitea returned invalid JSON"
            continue
        if not isinstance(repository, dict):
            errors[login] = "Gitea returned unexpected data"
            continue
        message = str(repository.get("message") or "")
        if message and is_network_error(message):
            raise RuntimeError(
                "network_access_required: rerun the same command with sandbox "
                f"network escalation: {message}"
            )
        if repository.get("full_name") == pull.slug:
            accessible.append(login)
        else:
            errors[login] = str(repository.get("message") or "repository not found")

    if len(accessible) > 1 and not requested:
        raise RuntimeError(
            f"multiple Tea logins can access {pull.slug}: {', '.join(accessible)}; "
            "pass --login"
        )
    if not accessible:
        detail = "; ".join(f"{name}: {error}" for name, error in errors.items())
        raise RuntimeError(
            f"no Tea login can access {pull.slug}; pass --login. {detail}"
        )
    return accessible[0]


class TeaClient:
    def __init__(self, pull: PullRef, login: str | None, retries: int = 2) -> None:
        self.pull = pull
        self.login = login
        self.retries = retries

    def login_args(self) -> list[str]:
        return ["--login", self.login] if self.login else []

    def run(self, *args: str) -> str:
        command = ["tea", *args]
        last_error = ""
        for attempt in range(self.retries + 1):
            process = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if process.returncode == 0:
                return process.stdout
            last_error = (process.stderr or process.stdout).strip()
            if is_network_error(last_error):
                raise RuntimeError(
                    "network_access_required: rerun the same command with sandbox "
                    f"network escalation: {last_error}"
                )
            if attempt < self.retries:
                time.sleep(0.4 * (2**attempt))
        raise RuntimeError(f"tea 调用失败: {' '.join(command[:3])}: {last_error}")

    def api_text(self, endpoint: str) -> str:
        return self.run(
            "api",
            *self.login_args(),
            "--repo",
            self.pull.slug,
            endpoint,
        )

    def api_json(self, endpoint: str) -> Any:
        output = self.api_text(endpoint)
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Gitea 返回的不是 JSON: {endpoint}") from error
        if isinstance(value, dict) and value.get("message") == "not found":
            raise FileNotFoundError(endpoint)
        if isinstance(value, dict) and is_network_error(
            str(value.get("message") or "")
        ):
            raise RuntimeError(
                "network_access_required: rerun the same command with sandbox "
                f"network escalation: {value['message']}"
            )
        return value

    def raw_optional(self, path: str, ref: str) -> str | None:
        endpoint = f"/repos/{self.pull.slug}/raw/{path}?ref={ref}"
        output = self.api_text(endpoint)
        if output.lstrip().startswith('{"message":"not found"'):
            return None
        return output


def pr_endpoint(pull: PullRef) -> str:
    return f"/repos/{pull.slug}/pulls/{pull.index}"


def extract_issue_refs(body: str, current: PullRef) -> list[tuple[str, str, int]]:
    refs: set[tuple[str, str, int]] = set()
    for owner, repo, number in re.findall(
        r"(?<![\w.-])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)", body
    ):
        refs.add((owner, repo, int(number)))
    for number in re.findall(r"(?<![\w/])#(\d+)", body):
        refs.add((current.owner, current.repo, int(number)))
    return sorted(refs)


def split_diff(diff: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            try:
                fields = shlex.split(line.rstrip("\n"))
                current = fields[3]
                if current.startswith("b/"):
                    current = current[2:]
            except (IndexError, ValueError):
                current = None
            if current is not None:
                sections[current] = [line]
            continue
        if current is not None:
            sections[current].append(line)
    return {path: "".join(lines) for path, lines in sections.items()}


def risk_tags(path: str) -> list[str]:
    value = path.lower()
    tags: list[str] = []
    groups = {
        "auth": ("auth", "token", "session", "login", "permission", "security"),
        "data": ("migration", "model", "schema", "serializer", "transaction"),
        "api": ("/api/", "viewset", "middleware", "interceptor", "request"),
        "config": ("settings", "config", "docker", "deploy"),
    }
    for tag, needles in groups.items():
        if any(needle in value for needle in needles):
            tags.append(tag)
    if re.search(r"(^|/)(tests?|__tests__)(/|$)|\.(test|spec)\.", value):
        tags.append("test")
    if (
        value.endswith((".lock", "-lock.yaml", "-lock.json", ".min.js", ".map"))
        or "generated" in value
    ):
        tags.append("generated")
    if value.endswith((".md", ".rst")):
        tags.append("docs")
    return tags


def file_score(file: dict[str, Any]) -> int:
    tags = set(file.get("risk_tags", []))
    score = 0
    score += 40 if "auth" in tags else 0
    score += 30 if "data" in tags else 0
    score += 20 if "api" in tags else 0
    score += 10 if "config" in tags else 0
    score += 5 if "test" in tags else 0
    score -= 50 if "generated" in tags else 0
    score -= 15 if tags == {"docs"} else 0
    score += min(int(file.get("changes") or 0), 500) // 50
    return score


def compact_review(review: dict[str, Any]) -> dict[str, Any]:
    reviewer = review.get("reviewer") or review.get("user") or {}
    if isinstance(reviewer, dict):
        reviewer_name = (
            reviewer.get("login") or reviewer.get("username") or reviewer.get("name")
        )
    else:
        reviewer_name = reviewer
    return {
        "id": review.get("id"),
        "reviewer": reviewer_name,
        "state": review.get("state"),
        "commit_id": review.get("commit_id") or review.get("commitId"),
        "submitted_at": review.get("submitted_at") or review.get("created_at"),
        "body": review.get("body") or "",
    }


def latest_own_review(
    reviews: list[dict[str, Any]], reviewer_login: str
) -> dict[str, Any] | None:
    own = [review for review in reviews if review.get("reviewer") == reviewer_login]
    return own[-1] if own else None


def choose_review_profile(
    stats: dict[str, Any], own_review: dict[str, Any] | None, head_sha: str
) -> dict[str, Any]:
    if own_review and own_review.get("commit_id") == head_sha:
        return {
            "lane": "focused-rereview",
            "reason": "The latest own review already targets this head; reassess only the prior finding or changed requirement.",
            "patch_mode": "none",
            "max_supplemental_rounds": 1,
            "size": "small",
            "worker": {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
        }

    files = int(stats.get("files") or 0)
    changed_lines = int(stats.get("changed_lines") or 0)
    high_risk_files = int(stats.get("high_risk_files") or 0)
    if files <= 20 and changed_lines <= 800 and high_risk_files <= 2:
        return {
            "lane": "fast",
            "reason": "Small change set (at most 20 files, 800 changed lines, and 2 high-risk files); review all relevant patches in one pass.",
            "patch_mode": "review-set",
            "max_supplemental_rounds": 1,
            "size": "small",
            "worker": {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
        }

    if files <= 50 and changed_lines <= 3_000 and high_risk_files <= 10:
        return {
            "lane": "medium",
            "reason": "Medium change set (at most 50 files, 3,000 changed lines, and 10 high-risk files); prioritize high-risk paths before one supplemental batch.",
            "patch_mode": "high-risk",
            "max_supplemental_rounds": 1,
            "size": "medium",
            "worker": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        }

    return {
        "lane": "large",
        "reason": "Large or cross-cutting change set; use maximum review depth with one supplemental batch.",
        "patch_mode": "high-risk",
        "max_supplemental_rounds": 1,
        "size": "large",
        "worker": {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
    }


def compact_snapshot(bundle: dict[str, Any], reviewer_login: str) -> dict[str, Any]:
    reviews = bundle.get("reviews", [])
    own_review = latest_own_review(reviews, reviewer_login)
    selected_reviews: list[dict[str, Any]] = []
    if own_review:
        selected_reviews.append(own_review)
    for review in reversed(reviews):
        if review not in selected_reviews:
            selected_reviews.append(review)
        if len(selected_reviews) == 2:
            break

    files = [
        {
            "filename": file.get("filename"),
            "status": file.get("status"),
            "changes": file.get("changes"),
            "risk_tags": file.get("risk_tags", []),
            "risk_score": file.get("risk_score", 0),
        }
        for file in bundle.get("files", [])
    ]
    commits = [
        {
            "sha": str(commit.get("sha") or "")[:12],
            "message": (commit.get("message") or "").strip(),
        }
        for commit in bundle.get("commits", [])
    ]
    instruction_manifest = [
        {
            "path": path,
            "chars": len(content),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }
        for path, content in bundle.get("instructions", {}).items()
    ]
    return {
        "schema_version": bundle.get("schema_version"),
        "pr": bundle.get("pr"),
        "review_profile": bundle.get("review_profile"),
        "stats": bundle.get("stats"),
        "commits": commits,
        "files": files,
        "reviews": selected_reviews,
        "issues": bundle.get("issues", []),
        "instruction_manifest": instruction_manifest,
        "since_last_own_review": bundle.get("since_last_own_review"),
        "cache": bundle.get("cache"),
        "commands": bundle.get("commands"),
    }


def select_review_files(files: list[dict[str, Any]]) -> list[str]:
    selected = [
        file
        for file in files
        if "generated" not in file.get("risk_tags", [])
        and set(file.get("risk_tags", [])) != {"docs"}
    ]
    selected.sort(
        key=lambda file: (
            "test" in file.get("risk_tags", []),
            -int(file.get("risk_score") or 0),
            file.get("filename") or "",
        )
    )
    return [str(file.get("filename")) for file in selected]


def fetch_file_pages(client: TeaClient) -> list[dict[str, Any]]:
    pull = client.pull
    first = client.api_json(
        f"/repos/{pull.slug}/pulls/{pull.index}/files?page=1&limit={PAGE_SIZE}"
    )
    if not isinstance(first, list):
        raise RuntimeError("文件列表格式异常: page=1")
    if len(first) < PAGE_SIZE:
        return first

    pages: list[dict[str, Any]] = list(first)
    page_numbers = list(range(2, MAX_FILE_PAGES + 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_FILE_PAGES) as pool:
        futures = {
            page: pool.submit(
                client.api_json,
                f"/repos/{pull.slug}/pulls/{pull.index}/files?page={page}&limit={PAGE_SIZE}",
            )
            for page in page_numbers
        }
        for page in page_numbers:
            value = futures[page].result()
            if not isinstance(value, list):
                raise RuntimeError(f"文件列表格式异常: page={page}")
            if value:
                pages.extend(value)
            if len(value) < PAGE_SIZE:
                break
    if len(pages) >= PAGE_SIZE * MAX_FILE_PAGES:
        raise RuntimeError(
            f"PR 超过 {PAGE_SIZE * MAX_FILE_PAGES} 个文件，请提高脚本分页上限"
        )
    return pages


def fetch_issues(
    login: str | None, refs: Iterable[tuple[str, str, int]], pull: PullRef
) -> list[dict[str, Any]]:
    refs = list(refs)
    if not refs:
        return []

    def fetch(ref: tuple[str, str, int]) -> dict[str, Any]:
        owner, repo, number = ref
        scoped = PullRef(pull.host, owner, repo, number, pull.url)
        client = TeaClient(scoped, login)
        value = client.api_json(f"/repos/{owner}/{repo}/issues/{number}")
        return {
            "repo": f"{owner}/{repo}",
            "number": number,
            "title": value.get("title"),
            "state": value.get("state"),
            "updated_at": value.get("updated_at"),
            "body": value.get("body") or "",
            "url": value.get("html_url") or value.get("url"),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(refs))) as pool:
        return list(pool.map(fetch, refs))


def instruction_candidates(files: list[dict[str, Any]], root_text: str) -> list[str]:
    paths = {"AGENTS.md"}
    for file in files:
        path = str(file.get("filename") or "")
        parts = Path(path).parts[:-1]
        for index in range(1, len(parts) + 1):
            paths.add(str(Path(*parts[:index]) / "AGENTS.md"))
    for value in re.findall(r"`([^`]+\.md)`", root_text):
        if not value.startswith(("http://", "https://", "/")):
            paths.add(value)
    return sorted(paths)[:30]


def cache_dir_for(pull: PullRef, head_sha: str) -> Path:
    return (
        cache_root()
        / safe_part(pull.host)
        / safe_part(pull.owner)
        / safe_part(pull.repo)
        / str(pull.index)
        / head_sha
    )


def classify_pull(pull: PullRef, login: str | None) -> dict[str, Any]:
    client = TeaClient(pull, login)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        user_future = pool.submit(client.api_json, "/user")
        pr_future = pool.submit(client.api_json, pr_endpoint(pull))
        files_future = pool.submit(fetch_file_pages, client)
        reviews_future = pool.submit(fetch_reviews, client)
        authenticated_user = user_future.result()
        pr = normalize_pr(pr_future.result(), pull)
        files = files_future.result()
        reviews = reviews_future.result()

    reviewer_login = authenticated_user.get("login") or authenticated_user.get(
        "username"
    )
    if not reviewer_login:
        raise RuntimeError("无法识别当前 Gitea 登录用户")
    head_sha = pr["head"]["sha"]
    if not head_sha:
        raise RuntimeError("PR 没有 head SHA")
    normalized_files = [
        {
            "filename": item.get("filename") or "",
            "additions": int(item.get("additions") or 0),
            "deletions": int(item.get("deletions") or 0),
            "risk_tags": risk_tags(str(item.get("filename") or "")),
        }
        for item in files
    ]
    for item in normalized_files:
        item["risk_score"] = file_score(item)
    stats = {
        "files": len(normalized_files),
        "additions": sum(item["additions"] for item in normalized_files),
        "deletions": sum(item["deletions"] for item in normalized_files),
        "changed_lines": sum(
            item["additions"] + item["deletions"] for item in normalized_files
        ),
        "diff_chars": None,
        "high_risk_files": sum(item["risk_score"] >= 30 for item in normalized_files),
    }
    own_review = latest_own_review(reviews, reviewer_login)
    return {
        "pr": {"url": pr["url"], "head": pr["head"]},
        "review_profile": choose_review_profile(stats, own_review, head_sha),
        "stats": stats,
        "cache_reused": False,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_pr(value: dict[str, Any], pull: PullRef) -> dict[str, Any]:
    return {
        "number": value.get("number") or value.get("index") or pull.index,
        "title": value.get("title"),
        "body": value.get("body") or "",
        "state": value.get("state"),
        "draft": value.get("draft"),
        "mergeable": value.get("mergeable"),
        "updated_at": value.get("updated_at") or value.get("updated"),
        "url": value.get("html_url") or pull.url,
        "base": {
            "ref": (value.get("base") or {}).get("ref"),
            "sha": (value.get("base") or {}).get("sha"),
        },
        "head": {
            "ref": (value.get("head") or {}).get("ref"),
            "sha": (value.get("head") or {}).get("sha"),
        },
    }


def fetch_reviews(client: TeaClient) -> list[dict[str, Any]]:
    value = client.api_json(
        f"/repos/{client.pull.slug}/pulls/{client.pull.index}/reviews?limit=50"
    )
    if not isinstance(value, list):
        raise RuntimeError("评审列表格式异常")
    return [compact_review(review) for review in value]


def fetch_compare(
    client: TeaClient, previous_sha: str | None, head_sha: str
) -> dict[str, Any] | None:
    if not previous_sha or previous_sha == head_sha:
        return None
    value = client.api_json(
        f"/repos/{client.pull.slug}/compare/{previous_sha}...{head_sha}"
    )
    files = value.get("files") or []
    return {
        "from": previous_sha,
        "to": head_sha,
        "total_commits": value.get("total_commits"),
        "files": [
            {
                "filename": file.get("filename"),
                "status": file.get("status"),
                "additions": file.get("additions"),
                "deletions": file.get("deletions"),
            }
            for file in files
        ],
    }


def build_snapshot(pull: PullRef, login: str | None, refresh: bool) -> dict[str, Any]:
    client = TeaClient(pull, login)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        user_future = pool.submit(client.api_json, "/user")
        pr_future = pool.submit(client.api_json, pr_endpoint(pull))
        authenticated_user = user_future.result()
        pr_value = pr_future.result()
    reviewer_login = authenticated_user.get("login") or authenticated_user.get(
        "username"
    )
    if not reviewer_login:
        raise RuntimeError("无法识别当前 Gitea 登录用户")
    pr = normalize_pr(pr_value, pull)
    head_sha = pr["head"]["sha"]
    if not head_sha:
        raise RuntimeError("PR 没有 head SHA")
    target = cache_dir_for(pull, head_sha)
    bundle_path = target / "bundle.json"
    diff_path = target / "full.diff"
    cached = bundle_path.exists() and diff_path.exists() and not refresh

    issue_refs = extract_issue_refs(pr["body"], pull)
    if cached:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            reviews_future = pool.submit(fetch_reviews, client)
            issues_future = pool.submit(fetch_issues, login, issue_refs, pull)
            reviews = reviews_future.result()
            issues = issues_future.result()
        bundle = load_json(bundle_path)
        bundle["pr"] = pr
        bundle["reviews"] = reviews[-5:]
        bundle["issues"] = issues
        stats = bundle.setdefault("stats", {})
        if "changed_lines" not in stats:
            stats["changed_lines"] = sum(
                int(item.get("additions") or 0) + int(item.get("deletions") or 0)
                for item in bundle.get("files", [])
            )
        bundle["schema_version"] = 2
        bundle["cache"]["reused_code_snapshot"] = True
    else:
        diff_endpoint = f"/repos/{pull.slug}/pulls/{pull.index}.diff"
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            commits_future = pool.submit(
                client.api_json,
                f"/repos/{pull.slug}/pulls/{pull.index}/commits?limit=100",
            )
            reviews_future = pool.submit(fetch_reviews, client)
            issues_future = pool.submit(fetch_issues, login, issue_refs, pull)
            files_future = pool.submit(fetch_file_pages, client)
            diff_future = pool.submit(client.api_text, diff_endpoint)
            root_instruction_future = pool.submit(
                client.raw_optional, "AGENTS.md", head_sha
            )
            commits = commits_future.result()
            reviews = reviews_future.result()
            issues = issues_future.result()
            files_raw = files_future.result()
            full_diff = diff_future.result()
            root_instruction = root_instruction_future.result() or ""

        patches = split_diff(full_diff)
        files = []
        for item in files_raw:
            path = item.get("filename") or ""
            normalized = {
                "filename": path,
                "status": item.get("status"),
                "additions": item.get("additions") or 0,
                "deletions": item.get("deletions") or 0,
                "changes": item.get("changes")
                or (item.get("additions") or 0) + (item.get("deletions") or 0),
                "risk_tags": risk_tags(path),
                "patch_chars": len(patches.get(path, "")),
            }
            normalized["risk_score"] = file_score(normalized)
            files.append(normalized)
        files.sort(key=lambda item: (-item["risk_score"], item["filename"]))

        candidates = instruction_candidates(files, root_instruction)
        instructions: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                path: pool.submit(client.raw_optional, path, head_sha)
                for path in candidates
                if path != "AGENTS.md"
            }
            if root_instruction:
                instructions["AGENTS.md"] = root_instruction
            for path, future in futures.items():
                content = future.result()
                if content:
                    instructions[path] = content

        stats = {
            "files": len(files),
            "additions": sum(item["additions"] for item in files),
            "deletions": sum(item["deletions"] for item in files),
            "changed_lines": sum(
                item["additions"] + item["deletions"] for item in files
            ),
            "diff_chars": len(full_diff),
            "high_risk_files": sum(item["risk_score"] >= 30 for item in files),
        }
        bundle = {
            "schema_version": 2,
            "pr": pr,
            "stats": stats,
            "commits": [
                {
                    "sha": commit.get("sha"),
                    "message": (commit.get("commit") or {}).get("message"),
                    "created": commit.get("created"),
                }
                for commit in commits
            ],
            "files": files,
            "reviews": reviews[-5:],
            "issues": issues,
            "instructions": instructions,
            "cache": {
                "directory": str(target),
                "diff": str(diff_path),
                "reused_code_snapshot": False,
                "head_sha256": hashlib.sha256(head_sha.encode()).hexdigest(),
            },
        }
        target.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(full_diff, encoding="utf-8")

    bundle["reviewer_login"] = reviewer_login
    own_review = latest_own_review(bundle.get("reviews", []), reviewer_login)
    previous_sha = own_review.get("commit_id") if own_review else None
    try:
        bundle["since_last_own_review"] = fetch_compare(client, previous_sha, head_sha)
    except (FileNotFoundError, RuntimeError):
        bundle["since_last_own_review"] = {
            "from": previous_sha,
            "to": head_sha,
            "unavailable": True,
        }
    bundle["review_profile"] = choose_review_profile(
        bundle.get("stats", {}), own_review, head_sha
    )
    bundle["commands"] = {
        "show_review_set": f"python3 review_pr.py show {pull.url} --review-set --max-chars 50000",
        "show_high_risk": f"python3 review_pr.py show {pull.url} --risk high",
        "show_files": f"python3 review_pr.py show {pull.url} --files PATH [PATH ...]",
        "submit": (
            f"python3 review_pr.py submit {pull.url} --expected-head {head_sha} "
            "--state approve|request-changes --body-file REVIEW.md"
        ),
    }
    write_json(bundle_path, bundle)
    return bundle


def build_review_packet(
    pull: PullRef, login: str | None, refresh: bool, max_chars: int
) -> dict[str, Any]:
    snapshot = build_snapshot(pull, login, refresh)
    head_sha = snapshot["pr"]["head"]["sha"]
    target = cache_dir_for(pull, head_sha)
    patches = split_diff((target / "full.diff").read_text(encoding="utf-8"))
    profile = snapshot.get("review_profile") or {}
    lane = str(profile.get("lane") or "medium")

    if lane == "fast":
        selected = select_review_files(snapshot.get("files", []))
    elif lane == "focused-rereview":
        selected = [
            str(item.get("filename"))
            for item in (snapshot.get("since_last_own_review") or {}).get("files", [])
            if item.get("filename")
        ]
    else:
        selected = [
            str(item.get("filename"))
            for item in snapshot.get("files", [])
            if int(item.get("risk_score") or 0) >= 30
            and "generated" not in item.get("risk_tags", [])
        ]
        if not selected:
            selected = select_review_files(snapshot.get("files", []))[:10]

    fragments: list[str] = []
    emitted = 0
    included: list[str] = []
    for path in selected:
        patch_text = patches.get(path)
        if not patch_text:
            continue
        if emitted + len(patch_text) > max_chars:
            break
        fragments.append(patch_text)
        included.append(path)
        emitted += len(patch_text)

    return {
        "snapshot": compact_snapshot(snapshot, snapshot["reviewer_login"]),
        "instructions": snapshot.get("instructions", {}),
        "initial_patch_files": included,
        "initial_patch": "".join(fragments),
        "initial_patch_limited": len(included) < len(selected),
    }


def latest_cache_dir(pull: PullRef) -> Path:
    root = (
        cache_root()
        / safe_part(pull.host)
        / safe_part(pull.owner)
        / safe_part(pull.repo)
        / str(pull.index)
    )
    candidates = [path for path in root.glob("*/bundle.json") if path.is_file()]
    if not candidates:
        raise FileNotFoundError("没有缓存，请先运行 snapshot")
    return max(candidates, key=lambda path: path.stat().st_mtime).parent


def show_cached(args: argparse.Namespace) -> None:
    pull = parse_pull_url(args.url)
    target = latest_cache_dir(pull)
    bundle = load_json(target / "bundle.json")
    if args.instructions:
        for path, content in bundle.get("instructions", {}).items():
            print(f"===== {path} =====")
            print(content.rstrip())
        return

    patches = split_diff((target / "full.diff").read_text(encoding="utf-8"))
    selected: list[str]
    if args.files:
        selected = args.files
    elif args.review_set:
        selected = select_review_files(bundle.get("files", []))
    else:
        threshold = 30 if args.risk == "high" else 10
        selected = [
            file["filename"]
            for file in bundle.get("files", [])
            if file.get("risk_score", 0) >= threshold
            and "generated" not in file.get("risk_tags", [])
        ]
    emitted = 0
    for path in selected:
        patch = patches.get(path)
        if not patch:
            print(f"===== {path} (patch unavailable) =====")
            continue
        if emitted + len(patch) > args.max_chars:
            print(
                f"===== output limited at {args.max_chars} chars; remaining files omitted ====="
            )
            break
        print(patch, end="" if patch.endswith("\n") else "\n")
        emitted += len(patch)


def read_review_body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return args.body
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError("请使用 --body、--body-file 或 stdin 提供评审正文")


def submit_review(args: argparse.Namespace) -> None:
    pull = parse_pull_url(args.url)
    client = TeaClient(pull, args.login)
    pr = normalize_pr(client.api_json(pr_endpoint(pull)), pull)
    head_sha = pr["head"]["sha"]
    if head_sha != args.expected_head:
        raise RuntimeError(
            f"head 已变化，拒绝提交: expected={args.expected_head}, actual={head_sha}"
        )
    body = read_review_body(args).strip()
    if not body:
        raise ValueError("评审正文不能为空")
    if args.dry_run:
        print(
            json.dumps(
                {"head": head_sha, "state": args.state, "body": body},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.state in {"approve", "request-changes"}:
        action = "approve" if args.state == "approve" else "reject"
        result = client.run(
            "pulls",
            action,
            "--repo",
            pull.slug,
            *client.login_args(),
            "--output",
            "json",
            str(pull.index),
            body,
        ).strip()
    else:
        result = client.run(
            "api",
            *client.login_args(),
            "--repo",
            pull.slug,
            "--method",
            "POST",
            "--field",
            f"body={body}",
            "--field",
            "event=COMMENT",
            "--field",
            f"commit_id={head_sha}",
            f"/repos/{pull.slug}/pulls/{pull.index}/reviews",
        ).strip()

    reviews = fetch_reviews(client)
    latest = reviews[-1] if reviews else None
    print(
        json.dumps(
            {
                "result": result,
                "head": head_sha,
                "latest_review": latest,
                "multiline_body": bool(latest and "\n" in latest.get("body", "")),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="批量抓取、筛选并缓存 PR 数据")
    snapshot.add_argument("url")
    snapshot.add_argument("--login", help="Tea 登录配置；默认自动选择唯一可访问账号")
    snapshot.add_argument("--refresh", action="store_true", help="忽略同 head 缓存")
    snapshot.add_argument("--full", action="store_true", help="输出完整缓存清单")

    prepare = subparsers.add_parser(
        "prepare", help="一次生成审核上下文、指令和首批 patch"
    )
    prepare.add_argument("url")
    prepare.add_argument("--login", help="Tea 登录配置；默认自动选择唯一可访问账号")
    prepare.add_argument("--refresh", action="store_true", help="忽略同 head 缓存")
    prepare.add_argument("--max-chars", type=int, default=50_000)

    classify = subparsers.add_parser("classify", help="只输出 head 和评审通道")
    classify.add_argument("url")
    classify.add_argument("--login", help="Tea 登录配置；默认自动选择唯一可访问账号")
    classify.add_argument("--refresh", action="store_true", help="忽略同 head 缓存")

    show = subparsers.add_parser("show", help="从缓存输出选定 patch，不再请求网络")
    show.add_argument("url")
    show.add_argument("--files", nargs="+")
    show.add_argument(
        "--review-set", action="store_true", help="输出小 PR 的完整有效代码集"
    )
    show.add_argument("--risk", choices=["high", "medium"], default="high")
    show.add_argument("--instructions", action="store_true")
    show.add_argument("--max-chars", type=int, default=120_000)

    submit = subparsers.add_parser("submit", help="校验 head、提交评审并回读")
    submit.add_argument("url")
    submit.add_argument("--login", help="Tea 登录配置；默认自动选择唯一可访问账号")
    submit.add_argument("--expected-head", required=True)
    submit.add_argument(
        "--state", choices=["approve", "request-changes", "comment"], required=True
    )
    body_group = submit.add_mutually_exclusive_group()
    body_group.add_argument("--body")
    body_group.add_argument("--body-file")
    submit.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()
    try:
        if args.command in {"snapshot", "prepare", "classify", "submit"}:
            args.login = resolve_login(parse_pull_url(args.url), args.login)
        if args.command == "prepare":
            pull = parse_pull_url(args.url)
            packet = build_review_packet(pull, args.login, args.refresh, args.max_chars)
            print(json.dumps(packet, ensure_ascii=False, indent=2))
        elif args.command == "snapshot":
            pull = parse_pull_url(args.url)
            snapshot = build_snapshot(pull, args.login, args.refresh)
            output = (
                snapshot
                if args.full
                else compact_snapshot(snapshot, snapshot["reviewer_login"])
            )
            print(json.dumps(output, ensure_ascii=False, indent=2))
        elif args.command == "classify":
            pull = parse_pull_url(args.url)
            print(
                json.dumps(
                    classify_pull(pull, args.login), ensure_ascii=False, indent=2
                )
            )
        elif args.command == "show":
            show_cached(args)
        else:
            submit_review(args)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
