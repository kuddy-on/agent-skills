#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXTURE_SCRIPT="$ROOT/tests/benchmark/fixture.sh"
IMAGE="${BENCHMARK_IMAGE:-agent-skills-codex-benchmark:0.147.0}"
MODEL="${BENCHMARK_MODEL:-gpt-5.6-sol}"
REASONING_EFFORT="${BENCHMARK_REASONING_EFFORT:-medium}"
REPETITIONS="${BENCHMARK_REPETITIONS:-1}"
AUTH_FILE="${1:-}"

if [[ -z "$AUTH_FILE" ]]; then
  printf 'usage: %s AUTH_JSON\n' "$0" >&2
  exit 2
fi
AUTH_FILE="$(realpath "$AUTH_FILE")"
[[ -f "$AUTH_FILE" ]] || {
  printf 'auth file not found: %s\n' "$AUTH_FILE" >&2
  exit 2
}
[[ "$REPETITIONS" =~ ^[1-9][0-9]*$ ]] || {
  printf 'BENCHMARK_REPETITIONS must be a positive integer: %s\n' "$REPETITIONS" >&2
  exit 2
}
docker image inspect "$IMAGE" >/dev/null

RUN_ROOT="$(mktemp -d /tmp/agent-skills-review-ab.XXXXXX)"
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

measure_variant() {
  local variant="$1"
  local state_dir="$2"
  local repetition="$3"
  local entry repo_dir tea_home pr_url prompt network fixture_input
  local events_file stderr_file start_ns end_ns wall_ms exit_code
  local thread_id final_message command_calls collab_calls tool_events usage failed_events

  entry="$(jq -c '.entries[0]' "$state_dir/fixture.json")"
  repo_dir="$(jq -r '.repo_dir' <<<"$entry")"
  tea_home="$(jq -r '.tea_home' <<<"$entry")"
  pr_url="$(jq -r '.pr_url' <<<"$entry")"
  fixture_input="$(jq -c '.fixture_input' <<<"$entry")"
  network="$(jq -r '.network' "$state_dir/fixture.json")"

  common="Use the gitea-review skill at /opt/benchmark-skill/SKILL.md. Review $pr_url using Tea home /run/codex-benchmark/home. If there is no blocking defect, submit a formal multiline approval. Do not clone or run tests. Run prepare exactly once and submit exactly once. Report the reviewed head, final review state, and total tool calls."
  if [[ "$variant" == subagent ]]; then
    prompt="$common Follow the root workflow and delegate the complete review to exactly one fresh review worker; pass that worker the Tea home."
  else
    prompt="$common PR_REVIEW_WORKER. Execute the complete worker workflow directly in the current agent and do not delegate."
  fi

  events_file="$RUN_ROOT/review-r$repetition-$variant.jsonl"
  stderr_file="$RUN_ROOT/review-r$repetition-$variant.stderr"
  printf 'running review repetition=%s/%s variant=%s model=%s effort=%s\n' \
    "$repetition" "$REPETITIONS" "$variant" "$MODEL" "$REASONING_EFFORT" >&2
  start_ns="$(date +%s%N)"
  set +e
  docker run --rm --network "$network" \
    --tmpfs "/run/codex-benchmark:rw,exec,nosuid,nodev,uid=$(id -u),gid=$(id -g),mode=0700" \
    --mount "type=bind,src=$AUTH_FILE,dst=/run/secrets/codex-auth.json,readonly" \
    --mount "type=bind,src=$tea_home,dst=/run/secrets/tea-home,readonly" \
    --mount "type=bind,src=$repo_dir,dst=/workspace" \
    --mount "type=bind,src=$ROOT/skills/gitea-review,dst=/opt/benchmark-skill,readonly" \
    "$IMAGE" \
    --model "$MODEL" \
    --config "model_reasoning_effort=\"$REASONING_EFFORT\"" \
    "$prompt" >"$events_file" 2>"$stderr_file"
  exit_code=$?
  set -e
  end_ns="$(date +%s%N)"
  wall_ms=$(((end_ns - start_ns) / 1000000))

  thread_id="$(jq -sr '[.[] | select(.type == "thread.started") | .thread_id] | last // ""' "$events_file")"
  final_message="$(jq -sr '[.[] | select(.type == "item.completed" and .item.type == "agent_message") | .item.text] | last // ""' "$events_file")"
  command_calls="$(jq -sr '[.[] | select(.type == "item.started" and .item.type == "command_execution")] | length' "$events_file")"
  collab_calls="$(jq -sr '[.[] | select(.type == "item.started" and .item.type == "collab_tool_call")] | length' "$events_file")"
  tool_events=$((command_calls + collab_calls))
  usage="$(jq -sc '[.[] | select(.type == "turn.completed") | .usage] | last // {}' "$events_file")"
  failed_events="$(jq -sr '[.[] | select(.type == "turn.failed" or .type == "error")] | length' "$events_file")"

  jq -n \
    --arg variant "$variant" --argjson repetition "$repetition" \
    --arg model "$MODEL" --arg reasoning_effort "$REASONING_EFFORT" \
    --arg thread_id "$thread_id" --arg final_message "$final_message" \
    --argjson exit_code "$exit_code" --argjson wall_ms "$wall_ms" \
    --argjson command_calls "$command_calls" --argjson collab_calls "$collab_calls" \
    --argjson tool_events "$tool_events" --argjson failed_events "$failed_events" \
    --argjson usage "$usage" --argjson fixture_input "$fixture_input" \
    '{variant:$variant,repetition:$repetition,model:$model,
      reasoning_effort:$reasoning_effort,thread_id:$thread_id,
      exit_code:$exit_code,wall_ms:$wall_ms,command_calls:$command_calls,
      collab_calls:$collab_calls,tool_events:$tool_events,
      failed_events:$failed_events,usage:$usage,fixture_input:$fixture_input,
      final_message:$final_message}' \
    >>"$RUNS_FILE"
}

for repetition in $(seq 1 "$REPETITIONS"); do
  if ((RANDOM % 2 == 0)); then
    VARIANTS=(subagent local)
  else
    VARIANTS=(local subagent)
  fi
  for variant in "${VARIANTS[@]}"; do
    state_dir="$RUN_ROOT/review-r$repetition-$variant-fixture"
    ACTIVE_STATES+=("$state_dir")
    "$FIXTURE_SCRIPT" start "$state_dir" review "$variant" >/dev/null
    measure_variant "$variant" "$state_dir" "$repetition"
    "$FIXTURE_SCRIPT" verify "$state_dir" |
      jq --arg variant "$variant" --argjson repetition "$repetition" \
        '. + {variant:$variant,repetition:$repetition}' \
        >>"$VERIFICATIONS_FILE"
    "$FIXTURE_SCRIPT" stop "$state_dir" >/dev/null
  done
done

jq -n \
  --arg image "$IMAGE" --arg model "$MODEL" --arg effort "$REASONING_EFFORT" \
  --argjson repetitions "$REPETITIONS" \
  --slurpfile runs "$RUNS_FILE" --slurpfile verifications "$VERIFICATIONS_FILE" \
  -f "$ROOT/tests/benchmark/aggregate-review-ab.jq"
