#!/usr/bin/env bash
# Phase 4, part 2: the 18-unit grid and the merge.
#
#   NGPU=3 tmux new -s t4 'bash pod/02_grid.sh 2>&1 | tee logs/02_grid.log'
#
# Resumable by construction: a completed (level, arm, seed) is skipped on the strength of its
# partial existing, and layer 0 is cached. A preempted pod loses at most one unit, so re-running
# this after any interruption is always the right move.
set -euo pipefail

CORPUS="${CORPUS:-cedh}"
NGPU="${NGPU:-$(nvidia-smi -L 2>/dev/null | wc -l)}"
[ "${NGPU:-0}" -ge 1 ] || { echo "FATAL: set NGPU" >&2; exit 1; }

cd "$(dirname "$0")/.."
mkdir -p logs
say() { printf '\n\033[1m== %s\033[0m  %s\n' "$1" "$(date -u +%H:%M:%SZ)"; }

PRE="findings/t4_scaling_preflight_${CORPUS}.json"

# ── the pre-registration gate ────────────────────────────────────────────────
# The point predictions must be frozen from the pre-flight's measured example ratios BEFORE any
# model trains. THRESHOLDS.md being newer than the pre-flight output is the evidence that you
# looked at the counts and then wrote the predictions down — in that order. Training first and
# writing predictions afterwards produces the same files and means nothing.
[ -f "$PRE" ] || { echo "FATAL: $PRE missing — run pod/01_preflight.sh first" >&2; exit 1; }
if [ ! THRESHOLDS.md -nt "$PRE" ]; then
    cat >&2 <<EOF
FATAL: THRESHOLDS.md has not been updated since the pre-flight ran.

The frozen rule needs its point predictions instantiated from the measured example ratios in
$PRE, committed, before the grid trains anything. Running the grid first and
recording predictions afterwards yields byte-identical files and is worth nothing.

  1. Put the measured ratios and predictions in THRESHOLDS.md
  2. git commit THRESHOLDS.md alone, noting that only counts were observed
  3. re-run this script

Override with FORCE_UNGATED=1 only if you are re-running after the freeze already happened and
file mtimes were lost (e.g. a fresh clone).
EOF
    [ "${FORCE_UNGATED:-0}" = "1" ] || exit 1
    echo "WARNING: proceeding ungated because FORCE_UNGATED=1" >&2
fi

say "grid — 6 shards over $NGPU GPU(s), $(( (6 + NGPU - 1) / NGPU )) wave(s)"
i=0
for level in full subsample; do          # full first: it is the subsample's reference
    for seed in 42 43 44; do
        gpu=$(( i % NGPU ))
        echo "  launch shard $i: $level s$seed -> GPU $gpu"
        CUDA_VISIBLE_DEVICES="$gpu" MR_HF_DIR="/workspace/hf/w$i" \
            uv run python -m mr.t4_scaling --corpus "$CORPUS" \
                --levels "$level" --seeds "$seed" --no-merge \
                > "logs/shard${i}_${level}_s${seed}.log" 2>&1 &
        i=$(( i + 1 ))
        # Strict waves rather than a job queue: makes a GPU collision impossible with no
        # bookkeeping, and costs nothing because all six shards are the same three arms.
        if [ $(( i % NGPU )) -eq 0 ]; then wait; fi
    done
done
wait

FAILED=0
for f in logs/shard*.log; do
    grep -qiE "Traceback|FATAL|Error" "$f" && { echo "!! see $f"; FAILED=1; }
done
[ "$FAILED" = "0" ] || { echo "one or more shards reported an error; re-run to resume" >&2; exit 1; }
echo "grid complete $(date -u +%FT%TZ)" >> logs/STATUS

say "merge"
uv run python -m mr.t4_scaling --merge --corpus "$CORPUS"

say "collect"
tar czf "t4_scaling_results_${CORPUS}.tgz" \
    findings/t4_scaling.md findings/t4_scaling.json \
    "findings/t4_scaling_preflight_${CORPUS}.json" \
    findings/t4_scaling_partial_*.json \
    "findings/t4_replicate_floor_${CORPUS}."* 2>/dev/null || true
ls -lh "t4_scaling_results_${CORPUS}.tgz"
echo "done $(date -u +%FT%TZ)" >> logs/STATUS

cat <<EOF

Read findings/t4_scaling.md before pulling it down. Every (level, arm) that ran must have a row —
assert_levels_rendered raises otherwise — and each verdict row must show observed, the volume null,
the CI and the pooled seed sd.

Then download t4_scaling_results_${CORPUS}.tgz and STOP the pod (not terminate — stopping keeps the
volume, so the ~45 min layer-0 build survives if you need to come back).
EOF
