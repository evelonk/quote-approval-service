#!/bin/sh
# Run from any directory. No .env, host DB port or demo database is used.
set -eu
cd "$(dirname "$0")/.."
export CORE_REPORT_DIR="${CORE_REPORT_DIR:-$(mktemp -d /tmp/quote-core-results.XXXXXX)}"
mkdir -p "$CORE_REPORT_DIR"
export LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)"
project="quote-core-check-$$"
cleanup() {
    docker compose -p "$project" -f compose.test.yaml down --remove-orphans
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
git rev-parse HEAD > "$CORE_REPORT_DIR/checked-commit.txt"
git status --porcelain > "$CORE_REPORT_DIR/working-tree.txt"
docker compose -p "$project" -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
printf 'Results: %s\n' "$CORE_REPORT_DIR"
