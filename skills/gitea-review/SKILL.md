---
name: gitea-review
description: Review or re-review Gitea pull requests from `/review` commands or Gitea `/pulls/NUMBER` URLs, using Tea/Gitea API only, then submit and verify a formal review. Isolate each PR in one reusable sub-agent and route its reasoning effort by change size.
---

# Review Gitea PR

Use `scripts/review_pr.py` for routing, cached snapshots, bounded patch reads,
head guarding, submission, and read-back. The parent only routes; the dedicated
PR worker makes every code judgement. Never clone the repository or create a
worktree.

## Route once at the parent

Keep parent commentary to one concise skill-use/routing notice and the final
result. Do not inspect the PR, diff, CI, Issues, or repeat worker progress.

1. Derive a stable worker name such as `review_owner_repository_123`.
2. Run exactly one routing request:

   ```bash
   <skill-dir>/scripts/review_pr.py classify <PR_URL>
   ```

   Read only `pr.head`, `stats`, and `review_profile`. Classification uses
   additions plus deletions: `fast` is at most 20 files/800 changed lines/2
   high-risk files; `medium` is at most 50/3,000/10; anything larger is
   `large`.
3. For a new PR, spawn exactly one worker with no inherited conversation and
   the marker `PR_REVIEW_WORKER`. Pass only the PR URL, selected Tea login when
   applicable, classified head/lane, and the user's review requirement.
4. Use the exact `review_profile.worker.model` and
   `review_profile.worker.reasoning_effort`: fast or focused re-review uses
   `gpt-5.6-sol`/`medium`, medium uses `high`, and large uses `xhigh`.
5. Wait once and return the worker result without duplicating its analysis.
   Reuse the same worker for later reviews of this PR. If it is unavailable,
   create one replacement and reconstruct only this PR from Gitea.

If sub-agents are unavailable, disclose that isolation is unavailable and run
worker mode locally. When the prompt contains `PR_REVIEW_WORKER`, skip routing
and delegation and execute the worker workflow below.

## Prepare once at the worker

Run exactly once:

```bash
<skill-dir>/scripts/review_pr.py prepare <PR_URL> --max-chars 50000
```

The script selects the only Tea login that can access the repository. Use
`--login <profile>` only when explicitly selected or when multiple profiles
can access it. Keep credentials in Tea's store and out of prompts/output.

The script always accesses Gitea. In a managed sandbox, request network
escalation on the first invocation and run the executable directly. If an
escalated call reports `network_access_required`, report it without changing
profiles or claiming credentials expired.

Read `snapshot`, `instructions`, and `initial_patch` completely. They contain
the exact head, contracts, prior reviews, changed-file inventory, repository
instructions, lane, and initial code pass. Do not separately call `classify`,
`snapshot`, `show --instructions`, or the lane's initial `show` command.

- `focused-rereview`: reassess only the previous finding or clarification.
- `fast`: treat `initial_patch` as the complete bounded pass only when
  `initial_patch_limited` is false. When it is true, fetch every path in
  `initial_patch_omitted_files` in the one supplemental batch before approving;
  `initial_patch_truncated_files` identifies any partial patch already shown.
- `medium`/`large`: start with the supplied high-risk patches and inventory.

Allow at most one supplemental batch using one `show --files ...` command or
one batched Tea API request. Stop when each candidate blocker is reproduced or
disproved. Ignore CI unless explicitly requested; do not expand into unrelated
code merely to find an issue.

## Submit once

Use `request-changes` for a reproducible P0/P1 blocker, `approve` when none
remains, and `comment` only when requested. Do not block on style, optional
refactoring, or accepted/deferred behavior.

Submit concise multiline Markdown exactly once:

```bash
<skill-dir>/scripts/review_pr.py submit <PR_URL> \
  --expected-head <HEAD_SHA> \
  --state approve|request-changes|comment \
  --body '<MULTILINE_MARKDOWN>'
```

Use `latest_review` from that command to verify reviewer, state, head, and line
breaks; do not perform another read-back. Return only the outcome, actionable
findings or confirmed fixes, reviewed head, and review link.
