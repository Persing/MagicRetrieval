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
VISIBLE=$(nvidia-smi -L 2>/dev/null | wc -l)
NGPU="${NGPU:-$VISIBLE}"
[ "${NGPU:-0}" -ge 1 ] || { echo "FATAL: set NGPU" >&2; exit 1; }

# Clamp to what actually exists. NGPU is baked into the pod's start command, and pod args are
# immutable — so a pod resized from 3 GPUs to 1 (to fit on a host with little free capacity) would
# still hand workers CUDA_VISIBLE_DEVICES=1 and =2 for devices that are not there. Every worker
# after the first would die on an invalid device rather than on anything informative.
if [ "$VISIBLE" -ge 1 ] && [ "$NGPU" -gt "$VISIBLE" ]; then
    echo "NGPU=$NGPU but only $VISIBLE GPU(s) visible — clamping to $VISIBLE"
    NGPU="$VISIBLE"
fi

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

# Checked on CONTENT, not on file mtimes. The mtime version was wrong in the case that matters: a
# fresh `git clone` writes every file at the same instant, so THRESHOLDS.md is never newer than the
# pre-flight output and a correctly pre-registered run would be blocked — pushing whoever hit it
# straight to the override, which is how a gate stops meaning anything.
#
# This instead verifies that THRESHOLDS.md records the ratio *these* counts imply and the
# prediction that ratio yields. That is a stronger claim than "edited afterwards": it is evidence
# the predictions were derived from this pre-flight and not from some other run.
uv run python - <<PY || exit 1
import json, math, pathlib, sys

pre = json.loads(pathlib.Path("$PRE").read_text(encoding="utf-8"))
full = [r for r in pre.values() if r["level"] == "full"]
sub = [r for r in pre.values() if r["level"] == "subsample"]
if not full or not sub:
    sys.exit("FATAL: pre-flight output has no full/subsample rows")

arm = sorted(full[0]["examples"])[0]
f = sum(r["examples"][arm] for r in full) / len(full)
s = sum(r["examples"][arm] for r in sub) / len(sub)
ratio = f / s
pred = 0.00389 * math.log(ratio)

md = pathlib.Path("THRESHOLDS.md").read_text(encoding="utf-8")
want = ("%.4f" % ratio, "%+.4f" % pred)
missing = [w for w in want if w not in md]
if missing:
    sys.exit(
        "FATAL: THRESHOLDS.md does not record the prediction these counts imply.\n"
        "  measured example ratio : %s\n"
        "  implied prediction     : %s\n"
        "  missing from THRESHOLDS.md: %s\n\n"
        "The frozen rule has to be instantiated from THIS pre-flight, and committed, before the\n"
        "grid trains anything. Recording predictions afterwards produces the same file and is\n"
        "worth nothing.\n"
        "  1. write the measured ratio and its prediction into THRESHOLDS.md\n"
        "  2. commit it alone, noting that only counts were observed\n"
        "  3. re-run this script" % (want[0], want[1], ", ".join(missing)))
print("pre-registration OK — THRESHOLDS.md records ratio %s -> prediction %s" % want)
PY

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
