#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

compile_cache="$(mktemp -d "${TMPDIR:-/tmp}/agent-skills-compile.XXXXXX")"
cleanup() {
  rm -rf -- "$compile_cache"
}
trap cleanup EXIT

for command in python3 ruff shellcheck shfmt; do
  if ! command -v "$command" >/dev/null; then
    printf 'required development command not found: %s\n' "$command" >&2
    exit 2
  fi
done

shell_files=()
while IFS= read -r -d '' file; do
  shell_files+=("$file")
done < <(
  python3 -c 'import pathlib, sys; [sys.stdout.write(f"{p}\0") for p in sorted(p for r in ("scripts", "skills", "tests") for p in pathlib.Path(r).rglob("*.sh") if p.is_file())]'
)

ruff format --check .
ruff check .
PYTHONPYCACHEPREFIX="$compile_cache" python3 -m compileall -q skills tests

if ((${#shell_files[@]})); then
  shfmt -d -i 2 -ci "${shell_files[@]}"
  shellcheck "${shell_files[@]}"
fi

printf 'format and static checks passed\n'
