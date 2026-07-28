#!/usr/bin/env bash
# Launch the MindFlock web UI.
#
#   ./run.sh            # local mode (DEFAULT): localhost only (binds 127.0.0.1)
#   ./run.sh local      # same as the default
#   ./run.sh tailscale  # reachable from any device signed into your
#                       #   tailnet (binds 0.0.0.0; auth gate on)
#
# Override the port:   PORT=9000 ./run.sh
#
# Either way the server prints a banner with the /m mobile URL (+ a scannable QR
# in tailscale mode). Access is always tailnet-private — a device must be signed
# into your Tailscale account to reach it; nothing is exposed to the internet.
#
# Want HTTPS over the tailnet instead of plain HTTP? Set it up once:
#     sudo tailscale set --operator=$USER
#     tailscale serve --bg 8765
# then launch with `./run.sh local` (serve proxies localhost) and the
# banner will switch to the https://<name>.ts.net/m URL automatically.
set -euo pipefail

# This script lives in backend/web/; the repo root is two levels up.
WEB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$WEB_DIR/../.." && pwd)"

PORT="${PORT:-8765}"
MODE="${1:-local}"

# Prefer the project venv; allow PY=... override (also handy for testing).
if [ -z "${PY:-}" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then PY="$REPO_ROOT/.venv/bin/python"; else PY="python3"; fi
fi

case "$MODE" in
  -h|--help|help)
    echo "usage: $(basename "$0") [local|tailscale]   (default: local; set port with PORT=NNNN)"
    exit 0
    ;;
esac

if [ "$MODE" = "tailscale" ] && ! command -v tailscale >/dev/null 2>&1; then
  echo "note: tailscale not found — install it, or use './run.sh local'" >&2
fi

# Single source of truth for host/port logic lives in run.py (it puts the repo
# root on sys.path itself); just delegate so the shell entrypoint and the Windows
# shortcut (which runs run.py) never drift.
exec "$PY" "$WEB_DIR/run.py" "$MODE" "$PORT"
