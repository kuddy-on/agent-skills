#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-}"
STATE_DIR="${2:-}"
CASE_NAME="${3:-merge}"
LANE="${4:-sample}"
IMAGE="${GITEA_TEST_IMAGE:-gitea/gitea:1.27.1}"
PASSWORD='AgentSkillsBenchmark-42!'
AGENT_GITEA_URL='http://gitea:3000'
INITIAL_COMMIT_DATE='2000-01-01T00:00:00Z'
FEATURE_COMMIT_DATE='2000-01-01T00:01:00Z'

if [[ -z "$ACTION" || -z "$STATE_DIR" ]]; then
  printf 'usage: %s start|verify|stop STATE_DIR [merge|review|release] [LANE]\n' "$0" >&2
  exit 2
fi

STATE_DIR="$(realpath -m "$STATE_DIR")"
FIXTURE="$STATE_DIR/fixture.json"

stop_fixture() {
  if [[ -f "$FIXTURE" ]]; then
    container="$(jq -r '.container' "$FIXTURE")"
    network="$(jq -r '.network' "$FIXTURE")"
    docker rm -f "$container" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
  fi
  rm -rf "$STATE_DIR"
}

if [[ "$ACTION" == stop ]]; then
  stop_fixture
  exit 0
fi

if [[ "$ACTION" == verify ]]; then
  [[ -f "$FIXTURE" ]] || {
    printf 'fixture not found: %s\n' "$FIXTURE" >&2
    exit 1
  }
  url="$(jq -r '.host_gitea_url' "$FIXTURE")"
  token="$(jq -r '.token' "$FIXTURE")"
  case_name="$(jq -r '.case' "$FIXTURE")"
  entry="$(jq -c '.entries[0]' "$FIXTURE")"
  repo="$(jq -r '.repo' <<<"$entry")"
  pr="$(jq -r '.pr' <<<"$entry")"
  state="$(curl --fail --silent --show-error \
    --header "Authorization: token $token" \
    "$url/api/v1/repos/owner/$repo/pulls/$pr")"

  if [[ "$case_name" == review ]]; then
    reviews="$(curl --fail --silent --show-error \
      --header "Authorization: token $token" \
      "$url/api/v1/repos/owner/$repo/pulls/$pr/reviews")"
    jq -n --argjson entry "$entry" --argjson state "$state" \
      --argjson reviews "$reviews" \
      '{entries: [$entry + {
        approved: ([$reviews[] | select(
          (.state | ascii_upcase) == "APPROVED" and
          ((.user.login // .user.username) == "reviewer") and
          (.commit_id == $state.head.sha)
        )] | length > 0),
        success: (
          $state.state == "open" and
          ([$reviews[] | select(
            (.state | ascii_upcase) == "APPROVED" and
            ((.user.login // .user.username) == "reviewer") and
            (.commit_id == $state.head.sha)
          )] | length > 0)
        )
      }]} | . + {success: .entries[0].success}'
  else
    jq -n --argjson entry "$entry" --argjson state "$state" \
      --arg case_name "$case_name" \
      '{entries: [$entry + {
        merged: $state.merged,
        merge_commit_sha: $state.merge_commit_sha,
        success: (
          if $case_name == "merge" then
            $state.merged == true and
            (($state.merge_commit_sha // "") | length > 0)
          else
            $state.merged == false and
            $state.state == "open" and
            $state.title == "chore(main): release 1.2.3" and
            $state.head.ref == "release-please--branches--main"
          end
        )
      }]} | . + {success: .entries[0].success}'
  fi
  exit 0
fi

[[ "$ACTION" == start ]] || {
  printf 'unknown action: %s\n' "$ACTION" >&2
  exit 2
}
[[ "$CASE_NAME" =~ ^(merge|review|release)$ ]] || {
  printf 'unknown benchmark case: %s\n' "$CASE_NAME" >&2
  exit 2
}
[[ ! -e "$STATE_DIR" ]] || {
  printf 'state directory already exists: %s\n' "$STATE_DIR" >&2
  exit 1
}

mkdir -p "$STATE_DIR"
CONTAINER="agent-skills-benchmark-${PPID}-$$"
NETWORK="agent-skills-benchmark-net-${PPID}-$$"
cleanup_on_error() {
  status=$?
  if ((status != 0)); then
    docker logs "$CONTAINER" >&2 2>/dev/null || true
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
    rm -rf "$STATE_DIR"
  fi
  exit "$status"
}
trap cleanup_on_error EXIT INT TERM

for command in curl docker git jq tea realpath sha256sum; do
  command -v "$command" >/dev/null || {
    printf 'required command not found: %s\n' "$command" >&2
    exit 1
  }
done

docker network create "$NETWORK" >/dev/null
docker run --detach --name "$CONTAINER" \
  --network "$NETWORK" --network-alias gitea \
  --publish 127.0.0.1::3000 \
  --env GITEA__database__DB_TYPE=sqlite3 \
  --env GITEA__database__PATH=/data/gitea/gitea.db \
  --env GITEA__security__INSTALL_LOCK=true \
  --env GITEA__service__DISABLE_REGISTRATION=true \
  "$IMAGE" >/dev/null

PORT="$(docker port "$CONTAINER" 3000/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')"
HOST_GITEA_URL="http://127.0.0.1:$PORT"
for _ in $(seq 1 60); do
  curl --fail --silent "$HOST_GITEA_URL/api/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl --fail --silent "$HOST_GITEA_URL/api/healthz" >/dev/null

docker exec --user git "$CONTAINER" gitea admin user create \
  --username owner --password "$PASSWORD" --email owner@example.test \
  --admin --must-change-password=false >/dev/null
TOKEN="$(docker exec --user git "$CONTAINER" gitea admin user generate-access-token \
  --username owner --token-name benchmark --scopes all --raw)"
docker exec --user git "$CONTAINER" gitea admin user create \
  --username reviewer --password "$PASSWORD" --email reviewer@example.test \
  --must-change-password=false >/dev/null
REVIEWER_TOKEN="$(docker exec --user git "$CONTAINER" gitea admin user generate-access-token \
  --username reviewer --token-name benchmark --scopes all --raw)"

api() {
  method="$1"
  endpoint="$2"
  data="${3:-}"
  args=(--fail --silent --show-error --request "$method"
    --header "Authorization: token $TOKEN" --header 'Content-Type: application/json')
  [[ -z "$data" ]] || args+=(--data "$data")
  curl "${args[@]}" "$HOST_GITEA_URL/api/v1$endpoint"
}

repo_name="benchmark-$CASE_NAME"
api POST /user/repos \
  "{\"name\":\"$repo_name\",\"private\":true,\"default_branch\":\"main\"}" >/dev/null
api PATCH "/repos/owner/$repo_name" \
  '{"default_branch":"main","has_pull_requests":true}' >/dev/null
api PUT "/repos/owner/$repo_name/collaborators/reviewer" \
  '{"permission":"write"}' >/dev/null

repo_dir="$STATE_DIR/repo"
tea_home="$STATE_DIR/tea-home"
mkdir -p "$repo_dir" "$tea_home"
git -C "$repo_dir" init --initial-branch=main >/dev/null
git -C "$repo_dir" config user.name 'Benchmark Test'
git -C "$repo_dir" config user.email benchmark@example.test
printf 'main\n' >"$repo_dir/content.txt"
if [[ "$CASE_NAME" == release ]]; then
  mkdir -p "$repo_dir/.gitea/workflows"
  printf 'name: Release\non: workflow_dispatch\njobs: {}\n' \
    >"$repo_dir/.gitea/workflows/release.yml"
fi
git -C "$repo_dir" add .
env GIT_AUTHOR_DATE="$INITIAL_COMMIT_DATE" \
  GIT_COMMITTER_DATE="$INITIAL_COMMIT_DATE" \
  git -C "$repo_dir" commit --message 'chore: initialize benchmark' >/dev/null
base_sha="$(git -C "$repo_dir" rev-parse HEAD)"
host_remote="$HOST_GITEA_URL/owner/$repo_name.git"
agent_remote="$AGENT_GITEA_URL/owner/$repo_name.git"
auth="$(printf 'owner:%s' "$TOKEN" | base64 | tr -d '\n')"
git -C "$repo_dir" remote add origin "$host_remote"
git -C "$repo_dir" -c "http.extraHeader=Authorization: Basic $auth" \
  push --set-upstream origin main >/dev/null

if [[ "$CASE_NAME" == release ]]; then
  feature_branch='release-please--branches--main'
  commit_title='chore(main): release 1.2.3'
  pr_title='chore(main): release 1.2.3'
else
  feature_branch='feature/benchmark'
  commit_title='feat(benchmark): compare paths'
  pr_title='feat(benchmark): compare paths'
fi
pr_body="Equivalent $CASE_NAME benchmark PR"
git -C "$repo_dir" switch --create "$feature_branch" >/dev/null
printf '%s\n' "$CASE_NAME" >>"$repo_dir/content.txt"
git -C "$repo_dir" add content.txt
env GIT_AUTHOR_DATE="$FEATURE_COMMIT_DATE" \
  GIT_COMMITTER_DATE="$FEATURE_COMMIT_DATE" \
  git -C "$repo_dir" commit --message "$commit_title" >/dev/null
head_sha="$(git -C "$repo_dir" rev-parse HEAD)"
git -C "$repo_dir" -c "http.extraHeader=Authorization: Basic $auth" \
  push --set-upstream origin "$feature_branch" >/dev/null
git -C "$repo_dir" remote set-url origin "$agent_remote"

repo_ready=false
for _ in $(seq 1 30); do
  repo_state="$(api GET "/repos/owner/$repo_name")"
  if jq -e '.empty == false and .default_branch == "main" and .has_pull_requests == true' \
    <<<"$repo_state" >/dev/null; then
    repo_ready=true
    break
  fi
  sleep 1
done
[[ "$repo_ready" == true ]] || {
  printf 'repository did not become ready: %s\n' "$repo_name" >&2
  exit 1
}
if [[ "$CASE_NAME" == merge ]]; then
  api POST "/repos/owner/$repo_name/statuses/$head_sha" \
    '{"state":"success","context":"benchmark","description":"benchmark CI passed"}' >/dev/null
fi
pr="$(api POST "/repos/owner/$repo_name/pulls" \
  "{\"title\":\"$pr_title\",\"head\":\"$feature_branch\",\"base\":\"main\",\"body\":\"$pr_body\"}" |
  jq -r '.number')"
login_token="$TOKEN"
[[ "$CASE_NAME" == review ]] && login_token="$REVIEWER_TOKEN"
HOME="$tea_home" tea login add --name benchmark --url "$HOST_GITEA_URL" \
  --token "$login_token" --no-version-check >/dev/null
tea_config="$tea_home/.config/tea/config.yml"
sed -i "s#$HOST_GITEA_URL#$AGENT_GITEA_URL#g" "$tea_config"

tree_sha="$(git -C "$repo_dir" rev-parse 'HEAD^{tree}')"
fixture_input="$(jq -n \
  --arg repo "$repo_name" --argjson pr "$pr" \
  --arg pr_url "$AGENT_GITEA_URL/owner/$repo_name/pulls/$pr" \
  --arg title "$pr_title" --arg body "$pr_body" \
  --arg base_ref main --arg head_ref "$feature_branch" \
  --arg base_sha "$base_sha" --arg head_sha "$head_sha" --arg tree_sha "$tree_sha" \
  '{repo:$repo,pr:$pr,pr_url:$pr_url,title:$title,body:$body,
    base_ref:$base_ref,head_ref:$head_ref,base_sha:$base_sha,
    head_sha:$head_sha,tree_sha:$tree_sha}')"
entry="$(jq -n --arg lane "$LANE" --arg repo "$repo_name" \
  --arg repo_dir "$repo_dir" --arg tea_home "$tea_home" \
  --arg pr_url "$AGENT_GITEA_URL/owner/$repo_name/pulls/$pr" \
  --arg case_name "$CASE_NAME" --argjson pr "$pr" \
  --argjson fixture_input "$fixture_input" \
  '{case:$case_name,lane:$lane,repo:$repo,repo_dir:$repo_dir,
    tea_home:$tea_home,pr_url:$pr_url,pr:$pr,fixture_input:$fixture_input}')"

jq -n --arg container "$CONTAINER" --arg network "$NETWORK" \
  --arg gitea_url "$AGENT_GITEA_URL" --arg host_gitea_url "$HOST_GITEA_URL" \
  --arg token "$TOKEN" --arg case_name "$CASE_NAME" --argjson entry "$entry" \
  '{case:$case_name,container:$container,network:$network,gitea_url:$gitea_url,
    host_gitea_url:$host_gitea_url,token:$token,entries:[$entry]}' >"$FIXTURE"
trap - EXIT INT TERM
jq 'del(.token, .host_gitea_url)' "$FIXTURE"
