#!/bin/sh
# Launch in the Pi's graphical login session; the voice service runs separately.
set -eu

if [ -z "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
    printf '%s\n' 'Doran display: run this launcher from the Pi Desktop graphical session.' >&2
    exit 1
fi

browser=''
for candidate in chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then
        browser="$candidate"
        break
    fi
done
if [ -z "$browser" ]; then
    printf '%s\n' 'Doran display: Chromium is unavailable. Voice care continues separately.' >&2
    exit 1
fi

display_host="${DISPLAY_HOST:-127.0.0.1}"
display_port="${DISPLAY_PORT:-8765}"

# No curl, Node, or extra Python package required. Wait for the real hub server.
python3 - "$display_host" "$display_port" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

host, raw_port = sys.argv[1:]
if host not in {"127.0.0.1", "localhost"}:
    sys.exit("Doran display: DISPLAY_HOST must be 127.0.0.1 or localhost.")
try:
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError
except ValueError:
    sys.exit("Doran display: DISPLAY_PORT must be an integer from 1 to 65535.")

url = f"http://{host}:{port}/api/state"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        with opener.open(url, timeout=2) as response:
            if response.status == 200 and isinstance(json.load(response), dict):
                break
    except (OSError, ValueError, urllib.error.URLError):
        pass
    time.sleep(1)
else:
    sys.exit("Doran display: hub server is unavailable after 120 seconds. Start main.py with DISPLAY_ENABLED=true, then rerun this launcher.")
PY

exec "$browser" \
    --kiosk \
    --no-first-run \
    --noerrdialogs \
    --user-data-dir="${XDG_CONFIG_HOME:-$HOME/.config}/doran-kiosk" \
    "http://$display_host:$display_port"
