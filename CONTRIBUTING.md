# Contributing

Thank you for contributing to Agent Skills.

## Before opening an issue

- Search existing issues and pull requests first.
- Use the appropriate issue form.
- Do not include credentials, private repository data, internal hostnames, or
  personal information.
- Report security vulnerabilities privately as described in `SECURITY.md`.

## Pull requests

1. Keep each pull request focused on one behavior or concern.
2. Explain the user-visible behavior and the reason for the change.
3. Add or update tests for behavior changes.
4. Update both READMEs when public usage or installation changes.
5. Confirm that generated output, caches, credentials, and local absolute paths
   are not included.

## Skill structure

Each skill lives in `skills/<skill-name>/` and must include a valid `SKILL.md`.
Put deterministic orchestration in `scripts/` and agent presentation metadata in
`agents/` only when needed. Keep skills independent unless a dependency is both
necessary and documented.

Do not embed Gitea credentials, organization-specific repository names, private
domains, or assumptions about a single installation. Prefer explicit inputs and
safe failure over hidden fallback behavior.

## Tests

Install Ruff, shfmt, and ShellCheck, then run repository-wide format and static
checks:

```bash
python -m pip install --requirement requirements-dev.txt
go install mvdan.cc/sh/v3/cmd/shfmt@v3.13.1
scripts/check.sh
```

Install ShellCheck with the operating system package manager before running the
script. The checks validate Python and Bash formatting, static errors, and
Python syntax. The unit suite also validates every skill's front matter, agent
metadata, script shebang, and executable mode.

Run the unit suite:

```bash
python -m unittest discover -s tests -v
```

For merge or Gitea API behavior, also run:

```bash
tests/integration/run.sh
```

The integration test requires Docker, Git, curl, jq, and Tea 0.14.0. It must use
only the temporary Gitea instance created by the test.

## Releases

Release Please runs after each push to `main`. It maintains a release pull
request from Conventional Commits, then creates the version tag and GitHub
Release after that pull request is merged. The `simple` releaser updates
`version.txt` and `CHANGELOG.md`.

The workflow uses its job-scoped `github.token`; no separate release secret is
required. Repository Actions settings must allow GitHub Actions to create and
approve pull requests. Checks created for a `GITHUB_TOKEN`-authored release
pull request require a maintainer with write access to approve them before they
run.

Merge a release pull request only after its required checks pass and all review
conversations are resolved. Use `fix:`, `feat:`, or a `!`/`BREAKING CHANGE`
footer to select patch, minor, or major SemVer increments.

## Style

- Target Python 3.11 or newer.
- Use LF line endings and UTF-8 text.
- Keep shell scripts compatible with Bash and enable strict error handling.
- Prefer standard-library Python unless an external dependency has a clear
  maintenance benefit.
- Keep documentation concise and examples free of private information.

By contributing, you agree that your contribution is licensed under the MIT
License included in this repository.
