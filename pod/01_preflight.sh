#!/usr/bin/env bash
# Phase 4, part 1: everything up to and including the counts-only pre-flight.
#
#   tmux new -s t4 'bash pod/01_preflight.sh 2>&1 | tee logs/01_preflight.log'
#
# Stops deliberately after the pre-flight. The point predictions have to be frozen from its measured
# example ratios BEFORE any model trains, and a script that ran straight through into the grid would
# destroy exactly the property the pre-registration exists to establish. `02_grid.sh` refuses to
# start until it can see that you did it.
#
# Everything here is resumable: layer 0, the arm text caches and the replicate floor are all
# skip-if-present. Re-running after a preemption costs only what had not finished.
set -euo pipefail

CORPUS="${CORPUS:-cedh}"
PIN_EXPECTED="2f61bf8d0f71a57303b2256a17304f95ee13fc912ce0018de4c553a3dd4537e0"

cd "$(dirname "$0")/.."
mkdir -p logs
say() { printf '\n\033[1m== %s\033[0m  %s\n' "$1" "$(date -u +%H:%M:%SZ)"; }
status() { echo "$1 $(date -u +%FT%TZ)" >> logs/STATUS; }

say "1/5 verification gate"
uv run pytest -q

PIN=$(uv run python -c "from mr import representations as R; print(R.load()['pin']['combined_sha256'])")
if [ "$PIN" != "$PIN_EXPECTED" ]; then
    echo "FATAL: parser pin is $PIN, expected $PIN_EXPECTED" >&2
    echo "The vendored clause pipeline differs from the one every committed finding was built on." >&2
    echo "Layer 1 would differ and every arm's text with it. Stop and reconcile." >&2
    exit 1
fi
echo "parser pin OK  $PIN"

uv run python - <<'PY'
import torch
n = torch.cuda.device_count()
assert n, "no CUDA device visible"
print(f"{n} GPU(s), torch {torch.__version__}, cuda {torch.version.cuda}")
for i in range(n):
    print(f"  [{i}] {torch.cuda.get_device_name(i)}")
PY
status "verified"

say "2/5 warm the shared caches (serial, CPU-bound, ~30-45 min)"
# Must precede any parallel work: these caches are written by whichever process arrives first, and
# `full` is the reference that every `subsample` level reads its strata and fingerprint from.
uv run python -m mr.t4_scaling --warm --corpus "$CORPUS"
status "warmed"

say "3/5 replicate floor on this stack (~25 min)"
# The pod's torch is the PyPI CUDA build, not the local cu130 — same version, different kernels — so
# the locally measured floor does not transfer. This is also the real speed measurement: arm A is
# 724 s per run on a local 4090, so the ratio here scales every shard estimate in RUNPOD.md.
if [ -f "findings/t4_replicate_floor_${CORPUS}.json" ]; then
    echo "already measured, skipping"
else
    uv run python -m mr.t4_representation --replicate A --corpus "$CORPUS" --seeds 42
fi
status "replicate-floor"

say "4/5 pre-flight — counts only, no model trained, no recall number"
uv run python -m mr.t4_scaling --preflight --corpus "$CORPUS"
status "preflight"

say "5/5 STOP — freeze the point predictions before running the grid"
cat <<'EOF'

The pre-flight above measured the realized training-example ratio per level. Nothing was trained and
no recall number exists yet, which is what makes freezing predictions against these counts still a
pre-registration rather than a description.

Do this now, on this machine or locally:

  1. Write the measured ratios and their point predictions into THRESHOLDS.md, under
     "T4 scaling — does deck diversity buy anything beyond pair volume?".
  2. Commit that alone, saying in the message that only counts were observed.
  3. Then run:  bash pod/02_grid.sh

02_grid.sh checks that THRESHOLDS.md is newer than the pre-flight output and refuses to start
otherwise. That check is the gate, not a formality.
EOF
