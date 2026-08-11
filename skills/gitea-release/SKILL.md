---
name: gitea-release
description: Publish Gitea repositories through an existing or newly updated Release Please pull request and a release workflow. Use when the user asks to publish or release a version, merge a Release Please PR, wait for a release generated after a known merge commit, or verify the resulting workflow, tag, and Gitea Release. Do not use for ordinary feature pull-request merging.
---

# Release Gitea Repository

Use [scripts/release.py](scripts/release.py) for Release Please polling, release-PR validation, rebase merge with a guarded administrator fallback, workflow polling, and Tag/Release verification. Do not reproduce this sequence with ad hoc `tea`, Git, sleep, or repeated status calls.

Run the publication directly in the current agent. Invoke the script as the first operational command with network escalation; do not create a subagent or worktree, inspect branches or worktree cleanliness, probe `tea`, or pre-fetch PR and workflow state. The script owns the complete deterministic workflow.

## Publish

When a Release Please PR already exists:

```bash
<skill-dir>/scripts/release.py --repo <repo>
```

Target a known Release Please PR when necessary:

```bash
<skill-dir>/scripts/release.py --repo <repo> --release-pr 123
```

- Add `--dry-run` to validate the selected release without merging it.
- Use `--base`, `--workflow`, or `--release-head-prefix` only when repository conventions differ from the defaults.
- Adjust polling and timeout only for known slow Gitea environments.
- Pass `--login <profile>` when the user selects a Tea profile. Otherwise the script selects the only configured profile for `origin` that has push access; it stops when the choice is ambiguous.
- Do not run `--dry-run` before an authorized publication unless the user explicitly requests a preview.

This Skill has no dependency on `gitea-merge` and does not inspect ordinary feature PRs or their commit SHAs. When ordinary PR merging is also requested, prefer `gitea-merge` if installed; otherwise let the agent perform that explicitly authorized merge with available Gitea tools. Invoke this Skill only after Release Please has created or updated the release PR.

## Workflow

1. Validate the worktree, `tea`, Git remote, repository, and release workflow.
2. Select one explicitly targeted Release Please PR, or require exactly one matching open PR.
3. Validate its branch, title, semantic version, state, and mergeability.
4. Rebase-merge the Release Please PR. Attempt one administrator force merge only when the target branch protection simultaneously enables CI status checks and requires at least one approval, the Release PR head has no CI status results at all, Gitea returns HTTP 405 because required statuses are missing, the unchanged PR remains conflict-free, and the selected Tea login has repository administrator permission.
5. Wait for the matching release workflow at the resulting base-branch SHA.
6. Require the workflow and every non-skipped job to succeed.
7. Verify the version Tag on `origin` and verify the Gitea Release when the API supports it.
8. Return a JSON summary with stage durations and URLs.

Do not run application tests during publication; the ordinary PR merge gate is separate. Do not clean feature branches. Treat a Gitea Release API error as a warning only after workflow and remote Tag success.

## Failure handling

- Do not retry an ambiguous merge command. Read the Release PR state first.
- Never force-merge an ordinary PR, a changed PR head, a conflicting PR, or a PR that no longer satisfies every Release Please validation rule.
- Attempt administrator force merge only once for a Release Please PR with zero CI status results that is blocked by the repository's combined CI-status and required-approval policy. Never force a PR with a pending, failed, or successful CI result, and never escalate approval-only failures, requested changes, official review requests, outdated branches, protected-file changes, server, network, merge-style, signing, work-in-progress, conflict-checking, or unknown failures. If either CI status checks or required approvals are not enabled, stop without forcing.
- Require one existing open Release Please PR instead of waiting indefinitely.
- On timeout or failure, report the current stage, PR/run URL, and exact conclusion.

## Sandbox network access

This script always accesses Gitea. In a managed sandbox, request network escalation on the first invocation; do not probe the network in the sandbox first. Invoke the executable directly rather than through `python3` so the user can persist an approval prefix scoped to this script. Run with `--repo "$(git rev-parse --show-toplevel)"`; do not pass a slug or URL. If an escalated call still returns `network_access_required`, report the network failure without switching Tea logins or declaring credentials expired.
