#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${GITEA_TEST_IMAGE:-gitea/gitea:1.27.1}"
CONTAINER="agent-skills-gitea-${PPID}-$$"
TMP_DIR="$(mktemp -d)"
PASSWORD='AgentSkillsTest-42!'

cleanup() {
  status=$?
  if (( status != 0 )); then
    docker logs "$CONTAINER" >&2 2>/dev/null || true
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
  exit "$status"
}
trap cleanup EXIT INT TERM

for command in curl docker git jq tea; do
  command -v "$command" >/dev/null || {
    printf 'required command not found: %s\n' "$command" >&2
    exit 1
  }
done

docker run --detach --name "$CONTAINER" \
  --publish 127.0.0.1::3000 \
  --env GITEA__database__DB_TYPE=sqlite3 \
  --env GITEA__database__PATH=/data/gitea/gitea.db \
  --env GITEA__security__INSTALL_LOCK=true \
  --env GITEA__service__DISABLE_REGISTRATION=true \
  "$IMAGE" >/dev/null

PORT="$(docker port "$CONTAINER" 3000/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')"
test -n "$PORT"
GITEA_URL="http://127.0.0.1:$PORT"

ready=false
for _ in $(seq 1 60); do
  if curl --fail --silent "$GITEA_URL/api/healthz" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  printf 'temporary Gitea did not become ready\n' >&2
  exit 1
fi

docker exec --user git "$CONTAINER" gitea admin user create \
  --username owner \
  --password "$PASSWORD" \
  --email owner@example.test \
  --admin \
  --must-change-password=false >/dev/null

TOKEN="$(docker exec --user git "$CONTAINER" gitea admin user generate-access-token \
  --username owner \
  --token-name integration \
  --scopes all \
  --raw)"
test -n "$TOKEN"

api() {
  method="$1"
  endpoint="$2"
  data="${3:-}"
  args=(
    --fail
    --silent
    --show-error
    --request "$method"
    --header "Authorization: token $TOKEN"
    --header 'Content-Type: application/json'
  )
  if [[ -n "$data" ]]; then
    args+=(--data "$data")
  fi
  curl "${args[@]}" "$GITEA_URL/api/v1$endpoint"
}

api POST /user/repos '{"name":"sandbox","private":true,"default_branch":"main"}' >/dev/null
api PATCH /repos/owner/sandbox '{"default_branch":"main","has_pull_requests":true}' >/dev/null

REPO_DIR="$TMP_DIR/repo"
TEA_HOME="$TMP_DIR/tea-home"
mkdir -p "$REPO_DIR" "$TEA_HOME"
git -C "$REPO_DIR" init --initial-branch=main >/dev/null
git -C "$REPO_DIR" config user.name 'Integration Test'
git -C "$REPO_DIR" config user.email integration@example.test
printf 'main\n' > "$REPO_DIR/content.txt"
git -C "$REPO_DIR" add content.txt
git -C "$REPO_DIR" commit --message 'chore: initialize repository' >/dev/null

REMOTE_URL="$GITEA_URL/owner/sandbox.git"
BASIC_AUTH="$(printf 'owner:%s' "$TOKEN" | base64 | tr -d '\n')"
git -C "$REPO_DIR" remote add origin "$REMOTE_URL"
git -C "$REPO_DIR" -c "http.extraHeader=Authorization: Basic $BASIC_AUTH" \
  push --set-upstream origin main >/dev/null

git -C "$REPO_DIR" switch --create feature/integration >/dev/null
printf 'feature\n' >> "$REPO_DIR/content.txt"
git -C "$REPO_DIR" add content.txt
git -C "$REPO_DIR" commit --message 'feat(integration): verify atomic merge' >/dev/null
FEATURE_HEAD="$(git -C "$REPO_DIR" rev-parse HEAD)"
git -C "$REPO_DIR" -c "http.extraHeader=Authorization: Basic $BASIC_AUTH" \
  push --set-upstream origin feature/integration >/dev/null

repo_ready=false
for _ in $(seq 1 30); do
  REPO_STATE="$(api GET /repos/owner/sandbox)"
  if jq --exit-status \
    '.empty == false and .default_branch == "main" and .has_pull_requests == true' \
    <<<"$REPO_STATE" >/dev/null; then
    repo_ready=true
    break
  fi
  sleep 1
done
if [[ "$repo_ready" != true ]]; then
  jq '{empty, default_branch, has_pull_requests, permissions}' <<<"$REPO_STATE" >&2
  exit 1
fi

api POST "/repos/owner/sandbox/statuses/$FEATURE_HEAD" \
  '{"state":"success","context":"integration","description":"temporary CI passed"}' >/dev/null
PR_NUMBER="$(api POST /repos/owner/sandbox/pulls \
  '{"title":"feat(integration): verify merge","head":"feature/integration","base":"main","body":"temporary integration PR"}' \
  | jq --raw-output '.number')"
test "$PR_NUMBER" -gt 0

HOME="$TEA_HOME" tea login add \
  --name integration \
  --url "$GITEA_URL" \
  --token "$TOKEN" \
  --no-version-check >/dev/null

RESULT="$(HOME="$TEA_HOME" "$ROOT/skills/gitea-merge/scripts/merge.py" \
  --repo "$REPO_DIR" \
  --pr "$PR_NUMBER" \
  --merge-strategy rebase)"
printf '%s\n' "$RESULT" | jq --exit-status \
  '.status == "success" and (.merge_commit_sha | length > 0)' >/dev/null
api GET "/repos/owner/sandbox/pulls/$PR_NUMBER" | jq --exit-status \
  '.merged == true and (.merge_commit_sha | length > 0)' >/dev/null

printf 'temporary Gitea integration test passed\n'
