---
name: gitea-review
description: Review or re-review Gitea pull requests from `/review` commands or Gitea `/pulls/NUMBER` URLs, using `tea` and Gitea API only, then submit and verify a formal multiline review. Use for Chinese or English requests such as review, 评审, 审核, 复审, 再看看, 已修复再审, or submit review comments. Isolate every new PR in its own fresh sub-agent, reuse that worker for the same PR, and automatically select a small, medium, large, or focused re-review lane.
---

# Review Gitea PR

Use `scripts/review_pr.py` for classification, cached snapshots, patch selection, head guarding, submission, and read-back. Keep code judgement in the dedicated PR worker.

## Isolate and classify

Apply these rules at the root agent:

1. Derive a stable worker name from owner, repository, and PR number, such as `review_owner_repository_123`.
2. Run `<skill-dir>/scripts/review_pr.py classify <PR_URL>`. Read only its head, statistics, and `review_profile`; do not inspect code in the root agent.
   - `changed_lines` means additions plus deletions. `diff_chars` is diagnostic only and must not affect classification.
   - Small (`fast`): at most 20 files, 800 changed lines, and 2 high-risk files.
   - Medium: at most 50 files, 3,000 changed lines, and 10 high-risk files.
   - Large: any limit above the medium thresholds is exceeded.
3. For a new PR, spawn exactly one worker with `fork_turns: "none"` and the marker `PR_REVIEW_WORKER`. Never pass unrelated PR history.
4. Always use `model: "gpt-5.6-sol"`. For `fast` or `focused-rereview`, use `reasoning_effort: "medium"`; for `medium`, use `reasoning_effort: "high"`; for `large`, use `reasoning_effort: "xhigh"`.
5. Reuse the existing worker with `followup_task` for every later review of the same PR. Pass the latest classification and user clarification. Do not create a replacement merely because the lane changed.
6. Let the worker fetch, inspect, decide, submit, and read back. Do not duplicate its review in the root agent.
7. If the previous worker is unavailable, create a fresh worker and reconstruct only this PR's history from Gitea. If sub-agents are unavailable, run worker mode locally and disclose that isolation was unavailable.

When the prompt contains `PR_REVIEW_WORKER`, execute the workflow below directly and do not delegate.

## Build the compact snapshot

Run:

```bash
<skill-dir>/scripts/review_pr.py snapshot <PR_URL>
```

The script automatically selects the only configured `tea` login that can access the PR repository. Pass `--login <profile>` when the user explicitly selects a profile or when multiple profiles can access it. Credentials must remain in the `tea` login store and must never be written into the Skill or command output.

This script always accesses Gitea for classification, snapshots, and submission. In a managed sandbox, request network escalation on the first networked invocation; do not probe the network in the sandbox first. Invoke the executable directly rather than through `python3` so the user can persist an approval prefix scoped to this script. If an escalated call still reports `network_access_required`, report the network failure without switching profiles or declaring credentials expired.

Read the compact snapshot completely: PR and Issue contracts, exact base/head, relevant previous reviews, complete changed-file inventory, repository-instruction manifest, incremental comparison, and `review_profile`. Do not run `snapshot --full` unless the compact output is structurally incomplete.

For a new worker, read every repository instruction once with:

```bash
<skill-dir>/scripts/review_pr.py show <PR_URL> --instructions
```

Remember the manifest hashes in this PR worker. On later reviews of the same PR, reread instructions only when a hash changes. Always read changed instruction files completely.

Use only Gitea API/`tea`. Do not clone, checkout, pull, or fetch the repository. Ignore CI unless explicitly requested.

## Follow the assigned lane

### Focused re-review

When `lane` is `focused-rereview`, the latest own review already targets the same head. Reassess only the previous finding or the user's changed requirement. Do not traverse the full patch again. Read one focused file only if the clarification cannot be resolved from the cached review context.

### Fast review

When `lane` is `fast`, run exactly one bounded code pass:

```bash
<skill-dir>/scripts/review_pr.py show <PR_URL> --review-set --max-chars 50000
```

Review the changed code and its tests together. Do not expand into unrelated routes, layouts, responsive behavior, dependencies, or architecture unless a changed line or current requirement creates a concrete failure path.

### Medium and large review

When `lane` is `medium` or `large`, start with:

```bash
<skill-dir>/scripts/review_pr.py show <PR_URL> --risk high
```

Use the file inventory to select the remaining paths most likely to affect the contract.

### Supplemental-read limit

After the lane's initial pass, allow at most one supplemental batch. Put every necessary path into one `show --files ...` command or one batched `tea api` call. Do not alternate repeatedly between reasoning and file retrieval.

Stop exploring when each candidate finding is either reproducible or disproved. Once the required pass finds no P0/P1, approve; do not search unrelated areas merely to find another issue.

## Decide and submit

Prioritize reproducible correctness, security, authorization, data integrity, concurrency, API-contract, migration, and regression defects.

- Use `request-changes` when a P0/P1 blocker remains.
- Use `approve` when no blocker remains; include only clearly non-blocking P2 notes.
- Use `comment` only when the user requests comments without a decision.

Do not block for CI, style preference, optional refactoring, or behavior explicitly accepted/deferred by the current product decision.

Write concise multiline Markdown with conclusion, blank lines, priority, trigger, impact, required direction, and reviewed head. Submit once:

```bash
<skill-dir>/scripts/review_pr.py submit <PR_URL> \
  --expected-head <HEAD_SHA> \
  --state approve|request-changes|comment \
  --body-file <REVIEW_BODY_FILE>
```

Use the command's `latest_review` to verify reviewer, state, head, and line breaks. Do not perform a separate read-back request. Return only the outcome, findings or confirmed fixes, reviewed head, and review link.
