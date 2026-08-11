---
name: gitea-release
description: Publish Gitea repositories through an existing or newly updated Release Please pull request and a release workflow. Use when the user asks to publish or release a version, merge a Release Please PR, wait for a release generated after a known merge commit, or verify the resulting workflow, tag, and Gitea Release. Do not use for ordinary feature pull-request merging.
---

# Release Gitea Repository

Use [scripts/release.py](scripts/release.py) for Release Please polling, release-PR validation, fixed rebase merge, workflow polling, and Tag/Release verification. Do not reproduce this sequence with ad hoc `tea`, Git, sleep, or repeated status calls.

Run the publication in one isolated subagent using model `gpt-5.6-sol` with reasoning effort `medium` when available. Otherwise run it in the current agent. Do not put model names in commits, PRs, comments, or releases.

## Publish

When a Release Please PR already exists:

```bash
<skill-dir>/scripts/release.py --repo <repo>
```

After an ordinary PR was merged, wait for a Release Please update that contains its merge commit:

```bash
<skill-dir>/scripts/release.py --repo <repo> --after-sha <merge_commit_sha>
```

Target a known Release Please PR when necessary:

```bash
<skill-dir>/scripts/release.py --repo <repo> --release-pr 123
```

- Add `--dry-run` to validate the selected release without merging it.
- Use `--base`, `--workflow`, or `--release-head-prefix` only when repository conventions differ from the defaults.
- Adjust polling and timeout only for known slow Gitea environments.
- Pass `--login <profile>` when the user selects a Tea profile. Otherwise the script selects the only configured profile for `origin` that has push access; it stops when the choice is ambiguous.

This Skill has no dependency on `gitea-merge`. When ordinary PR merging is also requested, prefer `gitea-merge` if installed; otherwise let the agent perform that explicitly authorized merge with available Gitea tools. Pass the resulting merge commit SHA through `--after-sha`.

## Workflow

1. Validate the worktree, `tea`, Git remote, repository, and release workflow.
2. If `--after-sha` is supplied, require that commit to be present on the base branch.
3. Find or wait for exactly one matching Release Please PR whose head contains that commit.
4. Validate its branch, title, semantic version, state, and mergeability.
5. Rebase-merge the Release Please PR with a fixed strategy.
6. Wait for the matching release workflow at the resulting base-branch SHA.
7. Require the workflow and every non-skipped job to succeed.
8. Verify the version Tag on `origin` and verify the Gitea Release when the API supports it.
9. Return a JSON summary with stage durations and URLs.

Do not run application tests during publication; the ordinary PR merge gate is separate. Do not clean feature branches. Treat a Gitea Release API error as a warning only after workflow and remote Tag success.

## Failure handling

- Do not retry an ambiguous merge command. Read the Release PR state first.
- Without `--after-sha`, require one existing open Release Please PR instead of waiting indefinitely.
- On timeout or failure, report the current stage, PR/run URL, and exact conclusion.

## Sandbox network access

This script always accesses Gitea. In a managed sandbox, request network escalation on the first invocation; do not probe the network in the sandbox first. Invoke the executable directly rather than through `python3` so the user can persist an approval prefix scoped to this script. Run with `--repo "$(git rev-parse --show-toplevel)"`; do not pass a slug or URL. If an escalated call still returns `network_access_required`, report the network failure without switching Tea logins or declaring credentials expired.
