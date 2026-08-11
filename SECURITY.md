# Security Policy

## Supported versions

This repository does not publish versioned releases. Security fixes are applied
to the latest commit on the `main` branch. Users should update installed skills
before reporting an issue that may already be fixed.

## Reporting a vulnerability

Do not open a public issue for suspected vulnerabilities, leaked credentials, or
privacy-sensitive findings.

Use GitHub's private vulnerability reporting for this repository:

https://github.com/kuddy-on/agent-skills/security/advisories/new

Include the affected skill, impact, reproduction steps, and any suggested
mitigation. Remove real credentials, private URLs, repository content, and other
sensitive data from the report whenever possible.

The maintainer will acknowledge the report, investigate it, and coordinate a fix
before public disclosure. Please do not disclose the issue publicly until a fix
or mitigation is available.

## Security model

- Credentials remain in the user's local tool configuration.
- Networked scripts must respect the agent runtime's sandbox and approval model.
- Merge and release operations validate immutable commit identities before
  changing remote state.
- Tests must never run against a user's existing Gitea repository.
