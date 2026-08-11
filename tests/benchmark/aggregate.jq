def percent_delta($with; $without):
  if $with == null or $without == null or $without == 0 then null
  else (((($with / $without) - 1) * 10000) | round) / 100
  end;

def median:
  sort as $values |
  ($values | length) as $count |
  if $count == 0 then null
  elif ($count % 2) == 1 then $values[($count / 2 | floor)]
  else (($values[($count / 2) - 1] + $values[$count / 2]) / 2)
  end;

def metrics($run): {
  success: $run.success,
  wall_ms: $run.wall_ms,
  tool_calls: $run.tool_calls,
  input_tokens: ($run.usage.input_tokens // 0),
  cached_input_tokens: ($run.usage.cached_input_tokens // 0),
  output_tokens: ($run.usage.output_tokens // 0)
};

[
  $runs[] as $run |
  ($verifications[] | select(
    .case == $run.case and
    .lane == $run.lane and
    .repetition == $run.repetition
  ) | .entries[0]) as $verification |
  $run + {
    server_success: $verification.success,
    success: (
      $run.exit_code == 0 and
      $run.failed_events == 0 and
      $run.tool_calls > 0 and
      $verification.success and
      ($run.case != "release" or ($run.final_message | contains("1.2.3")))
    )
  }
] as $enriched |
[
  $enriched | group_by([.case, .repetition])[] |
  (map(select(.lane == "with-skill"))[0]) as $with |
  (map(select(.lane == "without-skill"))[0]) as $without |
  {
    case: $with.case,
    repetition: $with.repetition,
    identical_fixture: ($with.fixture_input == $without.fixture_input),
    fixture_input: $with.fixture_input,
    with_skill: metrics($with),
    without_skill: metrics($without),
    delta_percent: {
      wall_ms: percent_delta($with.wall_ms; $without.wall_ms),
      tool_calls: percent_delta($with.tool_calls; $without.tool_calls),
      input_tokens: percent_delta(
        ($with.usage.input_tokens // 0);
        ($without.usage.input_tokens // 0)
      ),
      output_tokens: percent_delta(
        ($with.usage.output_tokens // 0);
        ($without.usage.output_tokens // 0)
      )
    }
  }
] as $pairs |
{
  image: $image,
  model: $model,
  reasoning_effort: $reasoning_effort,
  repetitions: $repetitions,
  tool_metric: "top-level actionable item.started events; child-agent internals are not emitted",
  runs: $enriched,
  verifications: $verifications,
  pairs: $pairs,
  summaries: [
    $pairs | group_by(.case)[] |
    . as $case_pairs |
    ($case_pairs | map(.with_skill.wall_ms)) as $with_wall |
    ($case_pairs | map(.without_skill.wall_ms)) as $without_wall |
    ($case_pairs | map(.with_skill.tool_calls)) as $with_tools |
    ($case_pairs | map(.without_skill.tool_calls)) as $without_tools |
    ($case_pairs | map(.with_skill.input_tokens)) as $with_input |
    ($case_pairs | map(.without_skill.input_tokens)) as $without_input |
    ($case_pairs | map(.with_skill.output_tokens)) as $with_output |
    ($case_pairs | map(.without_skill.output_tokens)) as $without_output |
    {
      case: $case_pairs[0].case,
      repetitions: ($case_pairs | length),
      all_success: ($case_pairs | map(
        .with_skill.success and .without_skill.success
      ) | all),
      identical_fixture_each_pair: ($case_pairs | map(.identical_fixture) | all),
      with_skill: {
        median_wall_ms: ($with_wall | median),
        mean_wall_ms: (($with_wall | add) / ($with_wall | length)),
        median_tool_calls: ($with_tools | median),
        total_input_tokens: ($with_input | add),
        total_output_tokens: ($with_output | add)
      },
      without_skill: {
        median_wall_ms: ($without_wall | median),
        mean_wall_ms: (($without_wall | add) / ($without_wall | length)),
        median_tool_calls: ($without_tools | median),
        total_input_tokens: ($without_input | add),
        total_output_tokens: ($without_output | add)
      },
      median_delta_percent: {
        wall_ms: percent_delta(
          ($with_wall | median);
          ($without_wall | median)
        ),
        tool_calls: percent_delta(
          ($with_tools | median);
          ($without_tools | median)
        )
      },
      aggregate_delta_percent: {
        wall_ms: percent_delta(($with_wall | add); ($without_wall | add)),
        input_tokens: percent_delta(($with_input | add); ($without_input | add)),
        output_tokens: percent_delta(($with_output | add); ($without_output | add))
      },
      pair_wall_delta_percent: ($case_pairs | map(.delta_percent.wall_ms))
    }
  ]
}
