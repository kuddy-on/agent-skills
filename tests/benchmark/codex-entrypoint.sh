#!/usr/bin/env bash
set -euo pipefail

AUTH_SOURCE=/run/secrets/codex-auth.json
TEA_SOURCE=/run/secrets/tea-home/.config/tea/config.yml
SKILL_SOURCE=/opt/benchmark-skill

if [[ ! -r "$AUTH_SOURCE" ]]; then
  printf 'required Codex auth file is not readable: %s\n' "$AUTH_SOURCE" >&2
  exit 2
fi
if [[ ! -r "$TEA_SOURCE" ]]; then
  printf 'required Tea config is not readable: %s\n' "$TEA_SOURCE" >&2
  exit 2
fi

umask 077
install -d -m 0700 \
  "$CODEX_HOME" \
  "$HOME/.agents/skills" \
  "$XDG_CONFIG_HOME/tea" \
  "$XDG_CACHE_HOME" \
  "$GITEA_REVIEW_CACHE" \
  "$TMPDIR"
install -m 0600 "$AUTH_SOURCE" "$CODEX_HOME/auth.json"
install -m 0600 "$TEA_SOURCE" "$XDG_CONFIG_HOME/tea/config.yml"

if [[ -e "$SKILL_SOURCE" ]]; then
  if [[ ! -r "$SKILL_SOURCE/SKILL.md" ]]; then
    printf 'mounted benchmark Skill has no readable SKILL.md: %s\n' \
      "$SKILL_SOURCE" >&2
    exit 2
  fi
  ln -s "$SKILL_SOURCE" "$HOME/.agents/skills/benchmark-target"
fi

exec codex \
  --sandbox danger-full-access \
  --ask-for-approval never \
  exec \
  --ephemeral \
  --ignore-user-config \
  --ignore-rules \
  --json \
  "$@"
