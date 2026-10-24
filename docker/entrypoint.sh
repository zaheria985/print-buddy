#!/bin/bash
set -euo pipefail
mkdir -p /data/hermes/state /data/archive /data/records /data/cache
# xvfb-run owns its display and cleanup; no fixed display or procps dependency.
if [[ ( -x /usr/bin/orca-slicer || -x /usr/bin/bambu-studio ) && -z "${DISPLAY:-}" ]]; then
    exec xvfb-run -a -s '-screen 0 1024x768x24' "$@"
fi
exec "$@"
