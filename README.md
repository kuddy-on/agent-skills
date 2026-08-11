# Agent Skills

[![Test](https://github.com/kuddy-on/agent-skills/actions/workflows/test.yml/badge.svg)](https://github.com/kuddy-on/agent-skills/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Reusable Agent Skills for reviewing, merging, and releasing repositories hosted
on Gitea.

[简体中文](README.zh-CN.md)

## Available skills

| Skill | Purpose |
| --- | --- |
| `gitea-review` | Review or re-review a Gitea pull request and submit a formal review. |
| `gitea-merge` | Validate and merge an ordinary pull request using rebase, squash, or merge commit. |
| `gitea-release` | Merge a Release Please pull request and verify its workflow, tag, and Gitea Release. |

Each skill can be installed and used independently. `gitea-release` does not
depend on `gitea-merge`. For a merge-and-release workflow, pass the ordinary
pull request's resulting commit SHA to `gitea-release` as `--after-sha`.

## Requirements

- An agent runtime that supports the [Agent Skills](https://agentskills.io/)
  format
- Git and Python 3.11 or newer
- [Tea](https://gitea.com/gitea/tea) configured for the target Gitea instance
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

All three skills access Gitea over the network. In a managed sandbox, the agent
must request network escalation on the first script invocation instead of first
running Tea in the restricted sandbox. The scripts are directly executable so
an approval can be scoped to the individual script rather than to a general
Python interpreter.

## Development

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
├── skills/
│   ├── gitea-merge/
│   ├── gitea-release/
│   └── gitea-review/
├── tests/
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
