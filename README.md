# Agent Skills

[![Test](https://github.com/kuddy-on/agent-skills/actions/workflows/test.yml/badge.svg)](https://github.com/kuddy-on/agent-skills/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Reusable Agent Skills for organizing workspaces and reviewing, merging, and
releasing repositories hosted on Gitea.

[简体中文](README.zh-CN.md)

## Available skills

| Skill | Purpose |
| --- | --- |
| `gitea-review` | Review or re-review a Gitea pull request and submit a formal review. |
| `gitea-merge` | Merge an ordinary pull request with gated rebase, squash, or merge commit; squash titles are Release Please-compatible, and merged feature branches are deleted by default. |
| `gitea-release` | Merge a Release Please pull request and verify its workflow, tag, and Gitea Release. |
| `organize-workspace` | Plan or inventory multi-project and operations workspaces, decide file ownership, and carry out authorized reorganization. |

Each skill can be installed and used independently. `gitea-release` does not
depend on `gitea-merge` and only operates on an existing Release Please pull
request.

`gitea-review` classifies PR size from metadata, then delegates the review to
one reusable worker with no inherited conversation. Fast and focused reviews
request `standard` capability, medium reviews `advanced`, and large reviews
`highest`. The agent selects a suitable model from the current runtime,
respecting user choices and constraints; no model name or reasoning setting is
pinned by the skill. The parent does not inspect the patch.

`organize-workspace` provides guidance without helper scripts or a Gitea
dependency. It respects existing layouts, preserves repository boundaries and
historical evidence, and distinguishes advice, read-only inventory, planning,
and authorized moves. Task navigation stays compact through grouped indexes
and on-demand reading of relevant records. Example request: "Use
$organize-workspace to inventory this workspace and propose a layout without
moving any files."

## Performance benchmark

On 2026-08-12, the isolated Docker benchmark ran three matched Skill/baseline
pairs for each workflow with `gpt-5.6-sol`, medium reasoning, and image
`agent-skills-codex-benchmark:0.147.0`. Every lane received a fresh Codex
tmpfs, Gitea instance, repository, and identical PR metadata and commit SHAs.
All 18 runs and server-state checks passed.

| Skill | Verified pairs | Mean time, Skill / baseline | Time | Input tokens | Output tokens | Median top-level calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gitea-merge` | 3/3 | 38.5s / 51.2s | -24.8% | -43.6% | -26.4% | 2 / 4 |
| `gitea-review` | 3/3 | 60.4s / 72.4s | -16.6% | -48.1% | -66.5% | 3* / 9 |
| `gitea-release` | 3/3 | 44.7s / 76.9s | -41.8% | -60.7% | -51.1% | 3 / 7 |

Per-round wall time is shown as Skill / baseline; negative percentages favor
the Skill:

| Skill | Round 1 | Round 2 | Round 3 |
| --- | ---: | ---: | ---: |
| `gitea-merge` | 44.7s / 61.8s (-27.7%) | 43.7s / 44.3s (-1.4%) | 27.2s / 47.6s (-42.8%) |
| `gitea-review` | 55.1s / 104.4s (-47.2%) | 54.7s / 63.0s (-13.0%) | 71.3s / 49.8s (+43.1%) |
| `gitea-release` | 40.3s / 79.1s (-49.1%) | 47.7s / 72.7s (-34.4%) | 46.2s / 78.8s (-41.3%) |

Only `auth.json` was mounted from the host; the Skill lane additionally
received its target Skill read-only. Local memory and caches were therefore
isolated, but server-side prompt caching cannot be disabled: reported cached
input tokens were nonzero and are included in the token comparisons above. The
runner randomizes lane order. `gitea-review` worker internals are not emitted
in parent JSONL, so its starred call count covers top-level events only. See
[the benchmark guide](tests/benchmark/README.md) for reproduction details.

## Requirements

- An agent runtime that supports the [Agent Skills](https://agentskills.io/)
  format
- For Gitea skills: Git, Python 3.11 or newer, and
  [Tea](https://gitea.com/gitea/tea) configured for the target Gitea instance
- For `organize-workspace`: filesystem access for inventory or moves, and Git
  when inspecting repositories; no Python or Tea dependency
- Node.js and npm when installing with the `skills` CLI

Credentials remain in Tea's local credential store. The skills never require
tokens, passwords, or private keys to be committed to this repository.

## Installation

Install all skills globally for Codex:

```bash
npx skills add kuddy-on/agent-skills --skill '*' --global --agent codex --yes
```

Install one skill:

```bash
npx skills add kuddy-on/agent-skills --skill gitea-merge --global --agent codex --yes
```

## Updating

```bash
npx skills update --global --yes
```

The update command refreshes already installed skills. Run the installation
command again when a skill is newly added or renamed.

## Sandbox permissions

The three Gitea skills access Gitea over the network. In a managed sandbox, the
agent must request network escalation on the first script invocation instead of first
running Tea in the restricted sandbox. The scripts are directly executable so
an approval can be scoped to the individual script rather than to a general
Python interpreter.

`organize-workspace` needs only the access required by the requested inventory
or moves. A layout proposal does not authorize filesystem changes or service
operations.

## Development

Install the development tools, including ShellCheck from the operating system
package manager, then run format and static checks:

```bash
python -m pip install --requirement requirements-dev.txt
go install mvdan.cc/sh/v3/cmd/shfmt@v3.13.1
scripts/check.sh
```

Run unit tests:

```bash
python -m unittest discover -s tests -v
```

Run the isolated integration test:

```bash
tests/integration/run.sh
```

The integration test creates a temporary local Gitea container and repository,
performs a real pull-request merge, and destroys all temporary resources when it
finishes. It never uses a configured production Gitea account.

## Repository structure

```text
agent-skills/
├── scripts/
├── skills/
│   ├── gitea-merge/
│   ├── gitea-release/
│   ├── gitea-review/
│   └── organize-workspace/
├── tests/
├── AGENTS.md
├── CONTRIBUTING.md
├── LICENSE
├── README.md
└── README.zh-CN.md
```

## Contributing and support

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request and
[SUPPORT.md](SUPPORT.md) for usage help. Please report vulnerabilities privately
according to [SECURITY.md](SECURITY.md).

## License

Licensed under the [MIT License](LICENSE).
