---
name: gitea-merge
description: Validate and merge ordinary Gitea pull requests with explicit or evidence-based rebase, squash, or merge-commit selection, Release Please-compatible squash titles, and default feature-branch deletion. Use when the user asks to merge a Gitea PR, choose a merge strategy, enforce CI and review gates, control a squash commit title, or preserve the merged branch. Do not use for merging Release Please PRs or publishing releases.
---

# Merge Gitea PR

Use [scripts/merge.py](scripts/merge.py) for validation, strategy selection, merging, and branch cleanup. Do not reproduce the sequence with ad hoc `tea` or Git commands when this Skill is available.

Run the script directly in the current agent as the first operational command. Do not create a subagent or worktree, inspect the repository, probe `tea`, or pre-fetch PR state. The script performs those checks in one process. Request sandbox network escalation on this first call.

## Run the merge

```bash
<skill-dir>/scripts/merge.py --repo <repo> --pr 123
```

- Pass `--merge-strategy rebase|squash|merge` when the user explicitly chooses a strategy.
- Keep `--merge-strategy auto` when the user does not choose.
- For squash merges, prefer a release-triggering PR title. If it does not qualify, select the highest-impact qualifying commit title: breaking `type!:`, then `feat`, `fix`, and `perf`.
- Pass `--squash-title <title>` only when the user explicitly supplies an alternative; it overrides both the PR and commit titles. Validate it with the same release-triggering rules. If no override, PR title, or commit title qualifies, stop before merging and ask the user to choose one.
- Delete the merged remote feature branch and its unchanged local ref by default.
- Add `--keep-branch` only when the user explicitly requests preserving the feature branch.
- Add `--dry-run` to inspect the decision without merging.
- Use `--skip-ci-check` only when the user explicitly authorizes merging without successful CI.
- Use `--base <branch>` only when the PR targets a branch other than `main`.
- Pass `--login <profile>` when the user selects a Tea profile. Otherwise the script selects the only configured profile for `origin` that has push access; it stops when the choice is ambiguous.
- Do not run `--dry-run` before an authorized real merge unless the user explicitly requests a preview.

Treat an explicit request to merge a specified PR as authorization for that merge and the default cleanup of its feature branch. Do not infer authorization to preserve the branch or bypass gates.

## Select the strategy

In `auto` mode:

- Select rebase for one commit.
- Select squash when multiple commits include fixup, WIP, debug, CI-repair, or review-only commits.
- Select rebase when clean conventional commits independently reference different Issues.
- Select squash when clean conventional commits all belong to one Issue.
- Select rebase when clean conventional commits have no Issue references but represent distinct scopes.
- Never auto-select merge commit.
- Return `decision_required` without merging when evidence is ambiguous.

An explicit user-selected strategy is authoritative.

## Enforce gates

Require the PR to target the configured base branch, have no conflict, have successful CI, and have no outstanding request-changes review. Preserve unrelated working-tree changes. Request remote branch deletion atomically with the merge unless `--keep-branch` is set. If the feature branch is checked out with uncommitted changes, keep its local ref while still deleting the remote branch.

Return the JSON summary, including the selected strategy, evidence, merged commit SHA, PR URL, cleanup result, and stage durations. When publication is also requested, wait until Release Please has created or updated its release PR, then invoke `gitea-release` if that Skill is available.

## Sandbox network access

This script always accesses Gitea. In a managed sandbox, request network escalation on the first invocation; do not probe the network in the sandbox first. Invoke the executable directly rather than through `python3` so the user can persist an approval prefix scoped to this script. Run with `--repo "$(git rev-parse --show-toplevel)"`; do not pass a slug or URL. If an escalated call still returns `network_access_required`, report the network failure without switching Tea logins or declaring credentials expired.
