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
PIN_EXPECTED="174bc3a164c5eae5fd473d0a4dc5fae49d73fbc7bb674bfe59630c3c28a5f8a0"

cd "$(dirname "$0")/.."
mkdir -p logs
say() { printf '\n\033[1m== %s\033[0m  %s\n' "$1" "$(date -u +%H:%M:%SZ)"; }
status() { echo "$1 $(date -u +%FT%TZ)" >> logs/STATUS; }

say "0/5 GPU availability"
# Checked before anything else, and loudly. A pod whose CUDA is unavailable will still run the whole
# test suite on CPU and fail somewhere confusing — the first time this happened it surfaced as a
# tie-break assertion in test_loo_eval, because torch.topk picks a different tie order on CPU, and
# the actual problem (device_count 0) was a warning three screens up.
#
# The retry loop is for the race, not the mismatch: a container can start before the GPU is attached.
for i in $(seq 1 30); do
    nvidia-smi >/dev/null 2>&1 && break
    [ "$i" = 1 ] && echo "waiting for nvidia-smi ..."
    sleep 2
done
nvidia-smi || echo "!! nvidia-smi is not working in this container"

# `nvidia-smi` working while `cuInit` fails is the signature of a missing /dev/nvidia-uvm node.
# Enumeration goes through the driver's management interface, which needs only /dev/nvidiactl and
# /dev/nvidia<N>; creating a CUDA context additionally needs the UVM device, which some container
# runtimes do not inject. So torch reports device_count=3 and is_available()=False at the same time.
echo "--- /dev/nvidia* ---"
ls -l /dev/nvidia* 2>&1 || echo "(no /dev/nvidia* nodes at all)"
if [ ! -e /dev/nvidia-uvm ]; then
    echo "/dev/nvidia-uvm MISSING — attempting to create it"
    if command -v nvidia-modprobe >/dev/null 2>&1; then
        nvidia-modprobe -u -c=0 && echo "nvidia-modprobe -u -c=0 ok" || echo "nvidia-modprobe failed"
    else
        # Fall back to creating the nodes by hand. 510 is the documented major for nvidia-uvm.
        major=$(grep -w nvidia-uvm /proc/devices | awk '{print $1}')
        if [ -n "$major" ]; then
            mknod -m 666 /dev/nvidia-uvm c "$major" 0 2>&1 && echo "created /dev/nvidia-uvm"
            mknod -m 666 /dev/nvidia-uvm-tools c "$major" 1 2>&1 || true
        else
            echo "nvidia-uvm not in /proc/devices — the kernel module is not loaded on the host"
        fi
    fi
    ls -l /dev/nvidia-uvm* 2>&1 || echo "still missing after the attempt"
fi

uv run python - <<'PY'
import os, sys, torch
print(f"torch {torch.__version__}  built against CUDA {torch.version.cuda}")
print(f"CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES')!r}")
n = torch.cuda.device_count()
print(f"torch.cuda.is_available()={torch.cuda.is_available()}  device_count={n}")
for i in range(n):
    print(f"  [{i}] {torch.cuda.get_device_name(i)}")
if n == 0:
    sys.exit(
        "\nFATAL: no CUDA device visible, so nothing here can train.\n"
        "Most likely a driver/runtime mismatch: torch on Linux pulls a CUDA 13 runtime\n"
        "(nvidia-cublas 13.x, nvidia-cudnn-cu13), which needs a host driver new enough for it.\n"
        "Compare the driver version nvidia-smi reports above against torch.version.cuda.\n"
        "Fix by choosing a pod image/host with a matching CUDA, not by pinning a different torch —\n"
        "the dependency set is frozen and shared with the local casual runs.")
PY

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
