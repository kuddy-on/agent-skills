#!/usr/bin/env python3
"""Verify observable outcomes for one isolated benchmark fixture."""

from __future__ import annotations

import json
import sys
from typing import Any


def is_multiline_approval(review: dict[str, Any], state: dict[str, Any]) -> bool:
    user = review.get("user") or {}
    body = review.get("body") or ""
    return (
        str(review.get("state") or "").upper() == "APPROVED"
        and (user.get("login") or user.get("username")) == "reviewer"
        and review.get("commit_id") == (state.get("head") or {}).get("sha")
        and bool(body)
        and "\n" in body
    )


def valid_release_evidence(
    entry: dict[str, Any], state: dict[str, Any], evidence: Any
) -> bool:
    if not isinstance(evidence, dict):
        return False
    release_pr = evidence.get("release_pr") or {}
    if not isinstance(release_pr, dict):
        return False
    common = (
        evidence.get("status") == "dry-run"
        and evidence.get("dry_run") is True
        and evidence.get("version") == "1.2.3"
        and evidence.get("workflow") == "release.yml"
        and release_pr.get("number") == entry.get("pr")
        and release_pr.get("expected_head") == (state.get("head") or {}).get("sha")
    )
    if not common:
        return False

    if entry.get("lane") == "with-skill":
        stages = evidence.get("stages") or []
        if not isinstance(stages, list):
            return False
        successful_stages = {
            stage.get("name")
            for stage in stages
            if isinstance(stage, dict)
            if stage.get("status") == "success"
        }
        return {
            "validate environment",
            "select Release PR",
            "validate Release PR",
        }.issubset(successful_stages)

    checks = evidence.get("checks") or {}
    if not isinstance(checks, dict):
        return False
    return all(
        checks.get(name) is True
        for name in ("only_open_release_pr", "release_metadata", "workflow_exists")
    )


def verify(payload: dict[str, Any]) -> dict[str, Any]:
    case_name = str(payload["case"])
    entry = dict(payload["entry"])
    state = dict(payload["state"])
    result = dict(entry)

    if case_name == "review":
        approved = any(
            is_multiline_approval(review, state)
            for review in payload.get("reviews") or []
        )
        result.update(
            {
                "approved": approved,
                "multiline_review": approved,
                "success": state.get("state") == "open" and approved,
            }
        )
    elif case_name == "merge":
        merged = state.get("merged") is True
        merge_sha = state.get("merge_commit_sha")
        result.update(
            {
                "merged": merged,
                "merge_commit_sha": merge_sha,
                "success": merged and bool(merge_sha),
            }
        )
    elif case_name == "release":
        evidence_valid = valid_release_evidence(entry, state, payload.get("evidence"))
        result.update(
            {
                "merged": state.get("merged"),
                "merge_commit_sha": state.get("merge_commit_sha"),
                "validation_evidence": evidence_valid,
                "success": (
                    state.get("merged") is False
                    and state.get("state") == "open"
                    and state.get("title") == "chore(main): release 1.2.3"
                    and (state.get("head") or {}).get("ref")
                    == "release-please--branches--main"
                    and evidence_valid
                ),
            }
        )
    else:
        raise ValueError(f"unknown benchmark case: {case_name}")

    return {"entries": [result], "success": result["success"]}


def main() -> int:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("verification input must be a JSON object")
    json.dump(verify(payload), sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
