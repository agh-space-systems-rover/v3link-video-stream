#!/usr/bin/env bash
# run_client.sh - start the operator-side viewer + control client.
# Usage:
#   ./run_client.sh <host-ip> [extra args...]
#
# Env overrides (defaults in parentheses):
#   VIDEO_PORT    video UDP port         (5000)
#   CONTROL_PORT  control TCP port       (9000)
set -euo pipefail

HOST="${1:?usage: ./run_client.sh <host-ip> [extra args...]}"
shift || true

VIDEO_PORT="${VIDEO_PORT:-5000}"
CONTROL_PORT="${CONTROL_PORT:-9000}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$DIR/stream_client.py" \
    --host "$HOST" \
    --video-port "$VIDEO_PORT" \
    --control-port "$CONTROL_PORT" \
    "$@"
