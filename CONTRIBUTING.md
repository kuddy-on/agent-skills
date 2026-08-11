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

## Style

- Target Python 3.11 or newer.
- Use LF line endings and UTF-8 text.
- Keep shell scripts compatible with Bash and enable strict error handling.
- Prefer standard-library Python unless an external dependency has a clear
  maintenance benefit.
- Keep documentation concise and examples free of private information.

By contributing, you agree that your contribution is licensed under the MIT
License included in this repository.
