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

On 2026-11-27 (commit `5dddb20`), the isolated Docker benchmark ran three
matched Skill/baseline pairs for each Gitea workflow with `gpt-6.2-sol`, medium
reasoning, and image `agent-skills-codex-benchmark:0.172.0`. The model and
reasoning setting were chosen for this controlled run only and are not pinned
defaults. `organize-workspace` is guidance-only and is not part of this
automated Gitea benchmark. Every lane received a fresh Codex tmpfs, Gitea
instance, repository, and identical PR metadata and commit SHAs. All 18 runs
and server-state checks passed.

| Skill | Verified pairs | Mean time, Skill / baseline | Time | Input tokens | Output tokens | Median top-level calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gitea-merge` | 3/3 | 18.1s / 32.2s | -43.6% | -45.5% | -66.4% | 2 / 5 |
| `gitea-review` | 3/3 | 40.5s / 45.4s | -10.7% | -35.6% | -60.7% | 3* / 9 |
| `gitea-release` | 3/3 | 20.2s / 43.3s | -53.3% | -36.0% | -76.3% | 3 / 9 |

Per-round wall time is shown as Skill / baseline; negative percentages favor
the Skill:

| Skill | Round 1 | Round 2 | Round 3 |
| --- | ---: | ---: | ---: |
| `gitea-merge` | 19.2s / 31.9s (-39.9%) | 17.6s / 33.8s (-48.1%) | 17.7s / 30.8s (-42.7%) |
| `gitea-review` | 41.9s / 41.9s (+0.0%) | 40.4s / 57.1s (-29.2%) | 39.1s / 37.0s (+5.5%) |
| `gitea-release` | 20.3s / 40.9s (-50.4%) | 19.6s / 50.9s (-61.4%) | 20.8s / 38.2s (-45.6%) |

Only `auth.json` was mounted from the host; the Skill lane additionally
received its target Skill read-only. Local memory and caches were therefore
isolated, but server-side prompt caching cannot be disabled: reported cached
input tokens were nonzero and are included in the token comparisons above. The
runner randomizes lane order. `gitea-review` worker internals are not emitted
in parent JSONL, so its starred call count and token totals cover top-level
events only. See [the benchmark guide](tests/benchmark/README.md) for
reproduction details.

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
