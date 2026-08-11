# Codex Benchmark Container

This image runs each Codex lane with fresh local state. It contains pinned
Codex and Tea binaries, but no credentials, user configuration, plugins,
repository data, or non-system Skills.

## Build

Build with the host user's IDs so bind-mounted fixture repositories remain
writable:

```bash
docker build \
  --build-arg BENCH_UID="$(id -u)" \
  --build-arg BENCH_GID="$(id -g)" \
  --tag agent-skills-codex-benchmark:0.147.0 \
  tests/benchmark
```

## Run a clean baseline

Use a fresh `docker run --rm` and a fresh fixture for every lane. The runner
creates a private Docker network where the fixture always resolves as
`http://gitea:3000`, so treatment and control receive the same PR URL.

```bash
docker run --rm --network "$(jq -r .network /absolute/path/to/fixture.json)" \
  --tmpfs /run/codex-benchmark:rw,exec,nosuid,nodev,uid=10000,gid=10000,mode=0700 \
  --mount type=bind,src=/absolute/path/to/auth.json,dst=/run/secrets/codex-auth.json,readonly \
  --mount type=bind,src=/absolute/path/to/lane/tea-home,dst=/run/secrets/tea-home,readonly \
  --mount type=bind,src=/absolute/path/to/lane/repo,dst=/workspace \
  agent-skills-codex-benchmark:0.147.0 \
  --model MODEL \
  --config 'model_reasoning_effort="medium"' \
  'PROMPT'
```

Replace the tmpfs UID and GID when the image was built with different IDs.
The entrypoint copies `auth.json` and Tea configuration into tmpfs, runs
`codex exec` with ephemeral sessions and ignored user configuration/rules, and
emits JSONL events for independent tool-call and token accounting.

## Run the Skill lane

Use the same command and prompt, adding exactly one read-only Skill mount:

```bash
--mount type=bind,src=/absolute/path/to/skills/gitea-merge,dst=/opt/benchmark-skill,readonly
```

Do not mount the benchmark state root, the host Codex home, user Skills,
plugins, caches, or Docker socket. Protect `auth.json` as a secret. A fresh
container removes local memory and cache effects; it does not eliminate model
variance, network variance, or server-side prompt caching. Record
`turn.completed.usage.cached_input_tokens`, randomize lane order, and run
multiple paired samples.

## Run the paired benchmark

Run isolated with/without-Skill pairs for all three cases:

```bash
tests/benchmark/run.sh /absolute/path/to/auth.json all
```

Set `BENCHMARK_MODEL`, `BENCHMARK_REASONING_EFFORT`, or `BENCHMARK_IMAGE` to
override the pinned defaults. The runner randomizes lane order, keeps Codex
JSONL and stderr outside the mounted repositories, verifies final Gitea state,
and emits one aggregate JSON document. Set `BENCHMARK_REPETITIONS=3` to run
three paired samples per case. Every lane gets a fresh Gitea instance with the
same internal URL, repository, PR number, fixed Git timestamps, and identical
base/head SHA. Set `BENCHMARK_KEEP=true` to retain raw artifacts under the
printed temporary directory for debugging.

To compare review delegation with local worker mode while loading the same
Skill in both lanes, run:

```bash
tests/benchmark/run-review-ab.sh /absolute/path/to/auth.json
```

This A/B test changes only the delegation mode. Child-agent internals may not
appear in the parent JSONL, so use end-to-end latency and Gitea verification as
the primary metrics.
