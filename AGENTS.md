# Repository Guidelines

## Project Structure & Module Organization

Each reusable skill lives in `skills/<skill-name>/`. A skill must provide a
`SKILL.md`; deterministic orchestration belongs in `scripts/`, and optional
agent-facing metadata belongs in `agents/`. Keep the three Gitea skills
independently installable.

Shared Python unit tests are in `tests/test_gitea_scripts.py`. The isolated,
Docker-backed Gitea scenario is `tests/integration/run.sh`; benchmark fixtures
live under `tests/benchmark/`. Repository policy and user documentation are in
the root Markdown files and `.github/`.

## Build, Test, and Development Commands

This repository has no build step. Use Python 3.11 or newer.

```bash
scripts/check.sh
```

Checks Python formatting and lint, Bash formatting and static errors, and
Python syntax. Install the tools listed in `CONTRIBUTING.md` first.

```bash
python -m unittest discover -s tests -v
```

Runs the complete unit suite used by CI.

```bash
tests/integration/run.sh
```

Starts a temporary local Gitea container and exercises a real merge. It
requires Docker, Git, curl, jq, and Tea 0.14.0; never point it at a production
instance.

## Coding Style & Naming Conventions

Follow `.editorconfig`: UTF-8, LF endings, a final newline, and no trailing
whitespace. Indent Python with four spaces; Bash, Markdown, and YAML with two.
Target Python 3.11+, prefer the standard library, type annotations, `Path`, and
clear domain-specific exceptions. Shell scripts must use Bash strict mode
(`set -euo pipefail`). Use lowercase kebab-case for skill directories and
snake_case for Python functions and tests. Ruff formats and lints Python;
shfmt and ShellCheck validate Bash.

## Testing Guidelines

Tests use the standard-library `unittest` framework. Name files `test_*.py`,
classes `*Tests`, and methods `test_<behavior>`. Add focused regression tests
for behavior changes and integration coverage for Gitea API or merge flows.
There is no stated coverage threshold; meaningful boundary and failure-path
coverage is expected.

## Commit & Pull Request Guidelines

History currently contains only an initial, concise Chinese commit, so no firm
commit convention is established. Prefer short imperative summaries;
Conventional Commit form is recognized by the tools, for example
`fix(merge): reject a changed PR head`.

Keep pull requests focused. Explain what changed and why, list validation, and
link relevant issues. Add tests for behavior changes. Update both `README.md`
and `README.zh-CN.md` when public usage changes. Complete the PR checklist and
exclude credentials, private URLs, personal data, caches, and absolute local
paths.

## Security & Skill Design

Never embed Gitea credentials or installation-specific assumptions. Keep
credentials in Tea's local store, use explicit inputs, fail safely, and follow
`SECURITY.md` for private vulnerability reports.
