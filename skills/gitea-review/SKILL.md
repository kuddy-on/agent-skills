---
name: gitea-review
description: Review or re-review Gitea pull requests from `/review` commands or Gitea `/pulls/NUMBER` URLs, using Tea/Gitea API only, then submit and verify a formal review. Isolate each PR in one reusable sub-agent and select an available model by capability tier.
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

   Read only `login`, `pr.head`, `stats`, and `review_profile`. Classification uses
   additions plus deletions: `fast` is at most 20 files/800 changed lines/2
   high-risk files; `medium` is at most 50/3,000/10; anything larger is
   `large`.
3. Read `review_profile.worker.capability_tier` and select the worker model
   using the runtime policy below. The profile specifies a capability tier,
   not a model identifier or a provider-specific reasoning setting.
4. For a new PR, spawn exactly one worker with no inherited conversation and
   the marker `PR_REVIEW_WORKER`. Pass only the PR URL, selected Tea login when
   applicable, classified head/lane/tier, runtime model selection, and the
   user's review requirement.
5. Wait once and return the worker result without duplicating its analysis.
   Reuse the same worker for later reviews of this PR. If it is unavailable,
   create one replacement and reconstruct only this PR from Gitea.

If sub-agents are unavailable, disclose that isolation is unavailable and run
worker mode locally. When the prompt contains `PR_REVIEW_WORKER`, skip routing
and delegation and execute the worker workflow below.

## Select a model at runtime

| Tier | Review scope | Capability needed |
| --- | --- | --- |
| `standard` | Fast or focused re-review | Reliable code review and instruction following for a bounded change. |
| `advanced` | Medium changes | Strong reasoning across files, contracts, and failure paths. |
| `highest` | Large changes | The strongest available code-review and reasoning capability for complex, cross-cutting changes. |

Choose autonomously from the models exposed by the current runtime, using
their capability descriptions and supported options. Respect explicit user
model choices, budget, and latency constraints. These tiers are relative
capability requirements, not permanent mappings to vendors, model families,
release dates, prices, or reasoning-effort names.

Inherit the current model when it meets the tier. Otherwise select a suitable
available model, without inventing an identifier or changing global settings.
If the runtime supports reasoning controls, choose a supported setting for
the task; otherwise retain its default. When the catalog is unavailable,
inherit the current model and disclose that the tier could not be verified.
If constraints prevent meeting the tier, state the limitation rather than
silently claiming an equivalent choice. Pass the selected model (or inherited
selection) and reasoning setting to the worker handoff. On reuse, reassess
whether the worker still meets the tier; if a change is needed but unsupported,
replace it using the PR reconstruction procedure above.

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
