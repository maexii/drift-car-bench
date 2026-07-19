#!/bin/bash
# Apply the local patches in patches/ to the vendored car-bench checkout and
# reinstall it into the venv. car-bench is a non-editable path dependency, so
# patching third_party/ alone has no effect until the package is reinstalled.
#
# Safe to re-run: already-applied patches and existing .env entries are skipped.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CAR_BENCH_DIR="$PROJECT_ROOT/third_party/car-bench"
PATCH_DIR="$PROJECT_ROOT/patches"

if [ ! -d "$CAR_BENCH_DIR" ]; then
    echo "car-bench not found at $CAR_BENCH_DIR" >&2
    echo "Run scripts/setup_car_bench.sh first." >&2
    exit 1
fi

for patch in "$PATCH_DIR"/car-bench-*.patch; do
    name="$(basename "$patch")"
    if git -C "$CAR_BENCH_DIR" apply --reverse --check "$patch" 2>/dev/null; then
        echo "✓ $name (already applied)"
    else
        git -C "$CAR_BENCH_DIR" apply "$patch"
        echo "✓ $name applied"
    fi
done

# The user-sim retry knobs are read from the environment; the evaluator loads
# .env via load_dotenv, so make sure sensible values are present there.
ENV_FILE="$PROJECT_ROOT/.env"
touch "$ENV_FILE"
if ! grep -q "^CAR_BENCH_USER_RETRY_ATTEMPTS=" "$ENV_FILE"; then
    printf 'CAR_BENCH_USER_RETRY_ATTEMPTS=3\n' >> "$ENV_FILE"
    echo "✓ added CAR_BENCH_USER_RETRY_ATTEMPTS=3 to .env"
fi
if ! grep -q "^CAR_BENCH_USER_RETRY_SLEEP_SECONDS=" "$ENV_FILE"; then
    printf 'CAR_BENCH_USER_RETRY_SLEEP_SECONDS=2,10,30\n' >> "$ENV_FILE"
    echo "✓ added CAR_BENCH_USER_RETRY_SLEEP_SECONDS=2,10,30 to .env"
fi

echo "Reinstalling car-bench into the venv..."
(cd "$PROJECT_ROOT" && uv sync --all-extras --reinstall-package car-bench)

echo ""
echo "✅ Patches applied and car-bench reinstalled."
