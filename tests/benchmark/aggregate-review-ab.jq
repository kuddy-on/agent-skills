def pct($a; $b):
  if $a == null or $b == null or $b == 0 then null
  else (((($a / $b) - 1) * 10000) | round) / 100
  end;

def median:
  sort as $values |
  ($values | length) as $count |
  if $count == 0 then null
  elif ($count % 2) == 1 then $values[($count / 2 | floor)]
  else (($values[($count / 2) - 1] + $values[$count / 2]) / 2)
  end;

[
  $runs[] as $run |
  ($verifications[] | select(
    .variant == $run.variant and .repetition == $run.repetition
  ) | .entries[0]) as $check |
  $run + {
    server_success: $check.success,
    success: ($run.exit_code == 0 and $run.failed_events == 0 and $check.success)
  }
] as $enriched |
[
  $enriched | group_by(.repetition)[] |
  (map(select(.variant == "subagent"))[0]) as $subagent |
  (map(select(.variant == "local"))[0]) as $local |
  {
    repetition: $subagent.repetition,
    identical_fixture: ($subagent.fixture_input == $local.fixture_input),
    subagent: $subagent,
    local: $local,
    local_vs_subagent_percent: {
      wall_ms: pct($local.wall_ms; $subagent.wall_ms),
      parent_tool_events: pct($local.tool_events; $subagent.tool_events),
      reported_input_tokens: pct(
        ($local.usage.input_tokens // 0);
        ($subagent.usage.input_tokens // 0)
      ),
      reported_output_tokens: pct(
        ($local.usage.output_tokens // 0);
        ($subagent.usage.output_tokens // 0)
      )
    }
  }
] as $pairs |
($pairs | map(.subagent.wall_ms)) as $subagent_wall |
($pairs | map(.local.wall_ms)) as $local_wall |
{
  image: $image,
  model: $model,
  reasoning_effort: $effort,
  repetitions: $repetitions,
  metric_note: "JSONL exposes parent events; child-agent internal events and usage may not be included",
  runs: $enriched,
  verifications: $verifications,
  pairs: $pairs,
  summary: {
    all_success: ($enriched | map(.success) | all),
    identical_fixture_each_pair: ($pairs | map(.identical_fixture) | all),
    subagent_median_wall_ms: ($subagent_wall | median),
    local_median_wall_ms: ($local_wall | median),
    local_median_delta_percent: pct(
      ($local_wall | median);
      ($subagent_wall | median)
    ),
    pair_wall_delta_percent: ($pairs | map(.local_vs_subagent_percent.wall_ms))
  }
}
