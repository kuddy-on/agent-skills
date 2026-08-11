#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXTURE_SCRIPT="$ROOT/tests/benchmark/fixture.sh"
CASES_FILE="$ROOT/tests/benchmark/case.json"
AGGREGATE_FILTER="$ROOT/tests/benchmark/aggregate.jq"
IMAGE="${BENCHMARK_IMAGE:-agent-skills-codex-benchmark:0.147.0}"
MODEL="${BENCHMARK_MODEL:-gpt-5.6-sol}"
REASONING_EFFORT="${BENCHMARK_REASONING_EFFORT:-medium}"
REPETITIONS="${BENCHMARK_REPETITIONS:-1}"
AUTH_FILE="${1:-}"
REQUESTED_CASE="${2:-all}"

if [[ -z "$AUTH_FILE" ]]; then
  printf 'usage: %s AUTH_JSON [all|merge|review|release]\n' "$0" >&2
  exit 2
fi
AUTH_FILE="$(realpath "$AUTH_FILE")"
[[ -f "$AUTH_FILE" ]] || {
  printf 'auth file not found: %s\n' "$AUTH_FILE" >&2
  exit 2
}
[[ "$REQUESTED_CASE" =~ ^(all|merge|review|release)$ ]] || {
  printf 'unknown benchmark case: %s\n' "$REQUESTED_CASE" >&2
  exit 2
}
[[ "$REPETITIONS" =~ ^[1-9][0-9]*$ ]] || {
  printf 'BENCHMARK_REPETITIONS must be a positive integer: %s\n' "$REPETITIONS" >&2
  exit 2
}

for command in docker jq realpath; do
  command -v "$command" >/dev/null || {
    printf 'required command not found: %s\n' "$command" >&2
    exit 1
  }
done
docker image inspect "$IMAGE" >/dev/null

RUN_ROOT="$(mktemp -d /tmp/agent-skills-benchmark-run.XXXXXX)"
RUNS_FILE="$RUN_ROOT/runs.ndjson"
VERIFICATIONS_FILE="$RUN_ROOT/verifications.ndjson"
ACTIVE_STATES=()

cleanup() {
  status=$?
  for state in "${ACTIVE_STATES[@]}"; do
    [[ ! -e "$state" ]] || "$FIXTURE_SCRIPT" stop "$state" >/dev/null 2>&1 || true
  done
  if [[ "${BENCHMARK_KEEP:-false}" == true ]]; then
    printf 'benchmark artifacts retained at %s\n' "$RUN_ROOT" >&2
  else
    rm -rf "$RUN_ROOT"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

if [[ "$REQUESTED_CASE" == all ]]; then
  CASES=(merge review release)
else
  CASES=("$REQUESTED_CASE")
fi

render_prompt() {
  local case_name="$1"
  local lane="$2"
  local pr_url="$3"
  local key template
  key="${lane//-/_}_prompt"
  template="$(jq -r --arg case "$case_name" --arg key "$key" \
    '.cases[] | select(.name == $case) | .[$key]' "$CASES_FILE")"
  template="${template//\{skill_dir\}/\/opt\/benchmark-skill}"
  template="${template//\{pr_url\}/$pr_url}"
  template="${template//\{repo_dir\}/\/workspace}"
  template="${template//\{tea_home\}/\/run\/codex-benchmark\/home}"
  printf '%s' "$template"
}

measure_lane() {
  local case_name="$1"
  local lane="$2"
  local case_state="$3"
  local repetition="$4"
  local entry repo_dir tea_home pr_url prompt skill_name network fixture_input
  local events_file stderr_file start_ns end_ns wall_ms exit_code
  local thread_id final_message tool_calls command_calls usage failed_events
  local mounts

  entry="$(jq -c '.entries[0]' "$case_state/fixture.json")"
  repo_dir="$(jq -r '.repo_dir' <<<"$entry")"
  tea_home="$(jq -r '.tea_home' <<<"$entry")"
  pr_url="$(jq -r '.pr_url' <<<"$entry")"
  fixture_input="$(jq -c '.fixture_input' <<<"$entry")"
  network="$(jq -r '.network' "$case_state/fixture.json")"
  prompt="$(render_prompt "$case_name" "$lane" "$pr_url")"
  skill_name="$(jq -r --arg case "$case_name" \
    '.cases[] | select(.name == $case) | .skill' "$CASES_FILE")"
  events_file="$RUN_ROOT/$case_name-r$repetition-$lane.jsonl"
  stderr_file="$RUN_ROOT/$case_name-r$repetition-$lane.stderr"

  mounts=(
    --mount "type=bind,src=$AUTH_FILE,dst=/run/secrets/codex-auth.json,readonly"
    --mount "type=bind,src=$tea_home,dst=/run/secrets/tea-home,readonly"
    --mount "type=bind,src=$repo_dir,dst=/workspace"
  )
  if [[ "$lane" == with-skill ]]; then
    mounts+=(--mount "type=bind,src=$ROOT/skills/$skill_name,dst=/opt/benchmark-skill,readonly")
  fi

  printf 'running case=%s repetition=%s/%s lane=%s model=%s effort=%s\n' \
    "$case_name" "$repetition" "$REPETITIONS" "$lane" "$MODEL" \
    "$REASONING_EFFORT" >&2
  start_ns="$(date +%s%N)"
  set +e
  docker run --rm --network "$network" \
    --tmpfs "/run/codex-benchmark:rw,exec,nosuid,nodev,uid=$(id -u),gid=$(id -g),mode=0700" \
    "${mounts[@]}" \
    "$IMAGE" \
    --model "$MODEL" \
    --config "model_reasoning_effort=\"$REASONING_EFFORT\"" \
    "$prompt" >"$events_file" 2>"$stderr_file"
  exit_code=$?
  set -e
  end_ns="$(date +%s%N)"
  wall_ms=$(((end_ns - start_ns) / 1000000))

  thread_id="$(jq -sr '[.[] | select(.type == "thread.started") | .thread_id] | last // ""' \
    "$events_file")"
  final_message="$(jq -sr '[.[] | select(.type == "item.completed" and .item.type == "agent_message") | .item.text] | last // ""' \
    "$events_file")"
  command_calls="$(jq -sr '[.[] | select(.type == "item.started" and .item.type == "command_execution")] | length' \
    "$events_file")"
  tool_calls="$(jq -sr '[.[] | select(
      .type == "item.started" and
      (.item.type == "command_execution" or
       .item.type == "mcp_tool_call" or
       .item.type == "web_search" or
       .item.type == "collab_tool_call")
    )] | length' "$events_file")"
  usage="$(jq -sc '[.[] | select(.type == "turn.completed") | .usage] | last // {}' \
    "$events_file")"
  failed_events="$(jq -sr '[.[] | select(.type == "turn.failed" or .type == "error")] | length' \
    "$events_file")"

  jq -n \
    --arg case "$case_name" \
    --arg lane "$lane" \
    --argjson repetition "$repetition" \
    --arg model "$MODEL" \
    --arg reasoning_effort "$REASONING_EFFORT" \
    --arg thread_id "$thread_id" \
    --arg final_message "$final_message" \
    --argjson exit_code "$exit_code" \
    --argjson wall_ms "$wall_ms" \
    --argjson tool_calls "$tool_calls" \
    --argjson command_calls "$command_calls" \
    --argjson failed_events "$failed_events" \
    --argjson usage "$usage" \
    --argjson fixture_input "$fixture_input" \
    '{
      case: $case,
      lane: $lane,
      repetition: $repetition,
      model: $model,
      reasoning_effort: $reasoning_effort,
      thread_id: $thread_id,
      exit_code: $exit_code,
      wall_ms: $wall_ms,
      tool_calls: $tool_calls,
      command_calls: $command_calls,
      failed_events: $failed_events,
      usage: $usage,
      fixture_input: $fixture_input,
      final_message: $final_message
    }' >>"$RUNS_FILE"
}

for case_name in "${CASES[@]}"; do
  for repetition in $(seq 1 "$REPETITIONS"); do
    if ((RANDOM % 2 == 0)); then
      LANES=(with-skill without-skill)
    else
      LANES=(without-skill with-skill)
    fi
    for lane in "${LANES[@]}"; do
      case_state="$RUN_ROOT/$case_name-r$repetition-$lane-fixture"
      ACTIVE_STATES+=("$case_state")
      "$FIXTURE_SCRIPT" start "$case_state" "$case_name" "$lane" >/dev/null
      measure_lane "$case_name" "$lane" "$case_state" "$repetition"
      "$FIXTURE_SCRIPT" verify "$case_state" |
        jq --arg case "$case_name" --arg lane "$lane" \
          --argjson repetition "$repetition" \
          '. + {case: $case, lane: $lane, repetition: $repetition}' \
          >>"$VERIFICATIONS_FILE"
      "$FIXTURE_SCRIPT" stop "$case_state" >/dev/null
    done
  done
done

jq -n \
  --arg image "$IMAGE" \
  --arg model "$MODEL" \
  --arg reasoning_effort "$REASONING_EFFORT" \
  --argjson repetitions "$REPETITIONS" \
  --slurpfile runs "$RUNS_FILE" \
  --slurpfile verifications "$VERIFICATIONS_FILE" \
  -f "$AGGREGATE_FILTER"
