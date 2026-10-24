#!/usr/bin/env bash
# One-shot: wait until the printer is no longer printing/paused, give post-print
# Discord prompts a grace period, then restart the gateway via the safe-restart helper.
#
# Moonraker comes from `services.moonraker_url` (config/local.yaml or legacy config/local.toml). The helper path comes from
# $HERMES_RESTART_HELPER or `paths.hermes_restart_helper`. $GRACE (seconds, default 900) is the wait.
set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO" || exit 1
MR=$(python3 -m print_buddy.settings get services.moonraker_url)
MR=${MR%/}
if [ -z "$MR" ]; then
  echo "services.moonraker_url is not configured; set it in config/local.yaml or config/local.toml" >&2
  exit 2
fi
HELPER=${HERMES_RESTART_HELPER:-$(python3 -m print_buddy.settings get paths.hermes_restart_helper)}
GRACE=${GRACE:-900}
state() { curl -fsS -m 10 "$MR/printer/objects/query?print_stats=state" | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["status"]["print_stats"]["state"])' 2>/dev/null || echo unknown; }
while :; do
  s=$(state)
  case "$s" in printing|paused|unknown) sleep 60 ;; *) break ;; esac
done
# A restart kills an intake mid-slice and leaves "Slicing." hanging in Discord. Wait for it.
INTAKES=${PRINT_BUDDY_INTAKE_DIR:-${HERMES_HOME:-$HOME/.hermes/profiles/3dprinting}/state/model-link-intakes}
intake_busy() {
  python3 - "$INTAKES" <<'PY'
import json, sys, time
from pathlib import Path
busy = {'INSPECTING_SOURCE', 'SLICE_APPROVED', 'SLICED', 'RESUMED'}
for f in Path(sys.argv[1]).glob('*.json'):
    try:
        if time.time() - f.stat().st_mtime < 1200 and json.loads(f.read_text()).get('status') in busy:
            print(f.stem[:12]); sys.exit(0)
    except Exception:
        pass
sys.exit(1)
PY
}
while id=$(intake_busy); do
  echo "intake $id is mid-flight; waiting"; sleep 30
done
echo "print state now: $s; waiting ${GRACE}s grace"
sleep "$GRACE"
s=$(state)
if [ "$s" = printing ] || [ "$s" = paused ]; then
  echo "a new print started during grace ($s); not restarting"; exit 1
fi
if id=$(intake_busy); then
  echo "intake $id started during grace; not restarting"; exit 1
fi
exec python3 "$HELPER" default
