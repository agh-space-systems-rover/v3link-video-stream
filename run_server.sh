#!/usr/bin/env bash
# run_server.sh - start the V3Link sender + control server on the Raspberry Pi.
# Usage:
#   sudo ./run_server.sh <operator-ip> [extra stream_server.py args...]
#
# Env overrides (defaults in parentheses):
#   PORT          video UDP port      (5000)
#   CONTROL_PORT  control TCP port    (9000)
#   WIDTH HEIGHT  resolution          (1024x768)
#   FPS           frames per second   (60)
#   BITRATE       H.264 bitrate bps   (4000000)
set -euo pipefail

HOST="${1:?usage: sudo ./run_server.sh <operator-ip> [extra args...]}"
shift || true

PORT="${PORT:-5000}"
CONTROL_PORT="${CONTROL_PORT:-9000}"
WIDTH="${WIDTH:-1024}"
HEIGHT="${HEIGHT:-768}"
FPS="${FPS:-60}"
BITRATE="${BITRATE:-4000000}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$DIR/stream_server.py" \
    --host "$HOST" \
    --port "$PORT" \
    --control-port "$CONTROL_PORT" \
    --width "$WIDTH" \
    --height "$HEIGHT" \
    --fps "$FPS" \
    --bitrate "$BITRATE" \
    "$@"
