#!/bin/bash
# Offline runtime acceptance for ONE already-built Print Buddy image. Never builds or pulls it.
#
#   tests/docker/image_acceptance.sh IMAGE [external|bundled] [OUT_DIR]
#
# IMAGE is a local tag or immutable ID. external (default) uses no Spoolman; bundled adds the
# optional docker-compose.spoolman.yml (its pinned Spoolman image must already be present).
# Everything runs under a unique Compose project on an internal network with no route out, with
# dummy secrets, and only this run's containers and temp files are removed afterwards. No
# Discord, printer, Home Assistant or household Spoolman is contacted, and no host port is used.
#
# Checks: image identity; packaged payload and handlers without a source mount; Orca executable
# and shared-library closure, or its absence; real slicing plus G-code guards, or fail-closed
# refusal; egress isolation; the same on a read-only root; per-service bot and listener readiness;
# stale and cross-container heartbeat refusal; graceful SIGTERM; data persistence across container
# recreation; a same-image tag switch (recreation coverage, NOT a cross-version upgrade); and,
# when bundled, Spoolman data persistence. Results: OUT_DIR/results.txt plus logs.
set -euo pipefail

image=${1:?usage: image_acceptance.sh IMAGE [external|bundled] [OUT_DIR]}
mode=${2:-external}
case "$mode" in external|bundled) ;; *) echo "mode must be external or bundled" >&2; exit 2 ;; esac
repo=$(cd "$(dirname "$0")/../.." && pwd)
out=${3:-$(mktemp -d)}
mkdir -p "$out"
run_id="$(date +%s)-$$"
project="pb-accept-${mode}-${run_id}"
work=$(mktemp -d)
data="$work/data"
failures=0

log() { echo "== $*"; }
record() {  # record NAME ok|FAIL DETAIL
    printf '%-34s %-4s %s\n' "$1" "$2" "$3" | tee -a "$out/results.txt"
    if [ "$2" != ok ]; then failures=$((failures + 1)); fi
}
collect() {
    compose ps -a > "$out/compose-ps.txt" 2>&1 || echo "compose ps failed" >> "$out/compose-ps.txt"
    compose logs --no-color > "$out/compose-logs.txt" 2>&1 || echo "compose logs failed" >> "$out/compose-logs.txt"
}
cleanup() {
    collect
    if ! compose down --remove-orphans > "$out/down.txt" 2>&1; then
        echo "warning: compose down failed for $project; see $out/down.txt" >&2
    fi
    docker image rm "pb-accept-tagswitch:$run_id" > /dev/null 2>&1 || true  # only this run's tag
    if ! rm -rf "$work" 2> /dev/null; then
        sudo rm -rf "$work" || echo "warning: could not remove $work" >&2
    fi
}
trap cleanup EXIT

# The deployment files a user downloads, plus the acceptance overlays, in a fresh folder.
cp "$repo/docker-compose.yml" "$repo/docker-compose.spoolman.yml" \
   "$repo/tests/docker/compose.acceptance.yml" "$repo/tests/docker/compose.acceptance-spoolman.yml" "$work/"
mkdir -p "$work/fixture" && cp "$repo/tests/docker/fake_moonraker.py" "$repo/tests/docker/image_probe.py" "$work/fixture/"
files=(-f docker-compose.yml)
if [ "$mode" = bundled ]; then files+=(-f docker-compose.spoolman.yml -f compose.acceptance-spoolman.yml); fi
files+=(-f compose.acceptance.yml)
compose() { (cd "$work" && docker compose -p "$project" "${files[@]}" "$@"); }

# .env from the shipped example: dummy values only, never real credentials.
dummy_token="offline-dummy-not-a-discord-token"
sed -e "s#^PRINT_BUDDY_IMAGE=.*#PRINT_BUDDY_IMAGE=$image#" \
    -e "s#^PRINT_BUDDY_DATA=.*#PRINT_BUDDY_DATA=$data#" \
    -e "s#^DISCORD_BOT_TOKEN=.*#DISCORD_BOT_TOKEN=$dummy_token#" \
    -e "s#^PRINT_BUDDY__DISCORD__CHAT_ID=.*#PRINT_BUDDY__DISCORD__CHAT_ID=100000000000000001#" \
    -e "s#^PRINT_BUDDY__DISCORD__ALLOWED_USER_IDS=.*#PRINT_BUDDY__DISCORD__ALLOWED_USER_IDS=200000000000000002#" \
    "$repo/.env.example" > "$work/.env"
export ACCEPTANCE_FIXTURES="$work/fixture" PRINT_BUDDY_IMAGE="$image"
mkdir -p "$data"/{hermes,archive,records,cache,spoolman}
if ! chown -R 1000:1000 "$data" 2> /dev/null; then
    sudo chown -R 1000:1000 "$data"
fi

log "image identity"
docker image inspect "$image" > "$out/image-inspect.json"
image_id=$(docker image inspect --format '{{.Id}}' "$image")
record identity ok "$image_id $(docker image inspect --format '{{.Os}}/{{.Architecture}} app-channel={{index .Config.Labels "org.print-buddy.app-channel"}}' "$image")"

probe_args=(--rm --network none --cap-drop ALL --security-opt no-new-privileges:true
    -e "DISCORD_BOT_TOKEN=$dummy_token" -e PRINT_BUDDY__BOT__MODE=standalone
    -e PRINT_BUDDY__DISCORD__CHAT_ID=100000000000000001 -e PRINT_BUDDY__DISCORD__ALLOWED_USER_IDS=200000000000000002
    -v "$data/hermes:/data/hermes" -v "$data/archive:/data/archive" -v "$data/records:/data/records"
    -v "$data/cache:/data/cache" -v "$work/fixture:/probe:ro")
probe() {  # probe LABEL [docker run options...] -- CHECK
    local label=$1; shift
    local opts=()
    while [ "$1" != -- ]; do opts+=("$1"); shift; done
    shift
    local name="$project-probe-$label-$1"
    if output=$(timeout 1200 docker run --name "$name" "${probe_args[@]}" "${opts[@]}" "$image" \
            python3 /probe/image_probe.py "$1" 2>&1); then
        record "$label:$1" ok "$(grep '^PROBE ' <<< "$output" | cut -c7-400)"
    else
        echo "$output" > "$out/probe-$label-$1.txt"
        record "$label:$1" FAIL "$(grep '^PROBE ' <<< "$output" | cut -c7-400 || tail -n 3 <<< "$output")"
    fi
}

for check in payload closure slice egress; do
    probe default -- "$check"
    probe readonly --read-only --tmpfs /tmp:size=64m -- "$check"
done
if output=$(timeout 300 docker run --name "$project-probe-botcheck" "${probe_args[@]}" "$image" \
        python3 -m print_buddy.bot check 2>&1); then
    record bot:offline-check ok "$(grep -c '^ok' <<< "$output") ok lines; $(grep 'handlers' <<< "$output" | cut -c1-80)"
else
    echo "$output" > "$out/bot-check.txt"
    record bot:offline-check FAIL "$(grep FAIL <<< "$output" | head -n 3 | tr '\n' ' ')"
fi

log "Unraid templates: their ExtraParams and PostArgs as docker run sees them"
for service in bot listener; do
    template="$repo/docker/unraid/print-buddy-$service.xml"
    mapfile -t extra < <(python3 -c "
import shlex, sys, xml.etree.ElementTree as ET
params = shlex.split(ET.parse(sys.argv[1]).getroot().findtext('ExtraParams'))
i = params.index('--restart'); del params[i:i + 2]   # --rm below replaces DockerMan's restart policy
print('\n'.join(params))" "$template")
    # The service module named by PostArgs must import under exactly these runtime flags.
    module=$(python3 -c "
import shlex, sys, xml.etree.ElementTree as ET
args = shlex.split(ET.parse(sys.argv[1]).getroot().findtext('PostArgs'))
assert args[:2] == ['python3', '-m'], args
print(args[2])" "$template")
    if output=$(timeout 300 docker run --name "$project-unraid-$service" "${probe_args[@]}" "${extra[@]}" "$image" \
            python3 -c 'import importlib, sys; importlib.import_module(sys.argv[1]); print("imported", sys.argv[1])' "$module" 2>&1); then
        record "unraid:$service-params" ok "docker run accepted ${#extra[@]} template arguments; $(tail -n 1 <<< "$output")"
    else
        echo "$output" > "$out/unraid-$service.txt"
        record "unraid:$service-params" FAIL "$(tail -n 2 <<< "$output" | tr '\n' ' ')"
    fi
done

log "listener readiness against the fake Moonraker"
if compose up -d --wait --wait-timeout 180 fake-moonraker listener > "$out/up-1.txt" 2>&1; then
    record listener:healthy ok "compose up --wait passed"
else
    record listener:healthy FAIL "see up-1.txt"
fi
connected=false
for _ in $(seq 30); do
    if compose exec -T listener python3 -c "import json, sys; d = json.load(open('/data/hermes/state/listener_health.json')); sys.exit(0 if d.get('moonraker_connected') else 1)" > /dev/null 2>&1; then
        connected=true
        break
    fi
    sleep 2
done
if [ "$connected" = true ]; then record listener:moonraker ok "heartbeat reports moonraker_connected"; else record listener:moonraker FAIL "never connected to fake-moonraker"; fi

log "bot: dummy token, no route out; it must not report healthy"
compose up -d bot > "$out/up-bot.txt" 2>&1 || echo "bot up returned non-zero" >> "$out/up-bot.txt"
bot_healthy=false
for _ in $(seq 20); do
    if compose exec -T bot python3 -m print_buddy.bot health > /dev/null 2>&1; then bot_healthy=true; break; fi
    if [ "$(docker inspect --format '{{.State.Running}}' "$(compose ps -aq bot)")" != true ]; then break; fi
    sleep 3
done
bot_state=$(docker inspect --format 'running={{.State.Running}} exit={{.State.ExitCode}}' "$(compose ps -aq bot)")
compose logs --no-color bot > "$out/bot.log" 2>&1 || echo "bot logs unavailable" >> "$out/bot.log"
bot_reason=$(grep -E 'Cannot connect to host|LoginFailure|Error:' "$out/bot.log" | tail -n 1 | sed 's/^[^|]*| *//' | cut -c1-160 || true)
if [ "$bot_healthy" = false ]; then record bot:fails-closed ok "$bot_state; never healthy without Discord; ${bot_reason:-see bot.log}"; else record bot:fails-closed FAIL "reported healthy with a dummy token"; fi

log "cross-service and cross-container heartbeat refusal"
if compose exec -T listener python3 -m print_buddy.bot health > "$out/listener-bot-health.txt" 2>&1; then
    record heartbeat:bot-in-listener FAIL "bot health passed inside the listener container"
else
    record heartbeat:bot-in-listener ok "$(grep -m 1 FAIL "$out/listener-bot-health.txt")"
fi
if compose run --rm --no-deps -T listener python3 -m print_buddy.healthcheck > "$out/foreign-health.txt" 2>&1; then
    record heartbeat:other-container FAIL "a second container accepted the listener's heartbeat"
else
    record heartbeat:other-container ok "$(grep -m 1 FAIL "$out/foreign-health.txt")"
fi

log "persistence marker, then graceful SIGTERM"
compose exec -T listener sh -c 'echo accepted > /data/archive/acceptance-marker && echo accepted > /data/records/acceptance-marker'
listener_before=$(compose ps -q listener)
start=$(date +%s)
compose stop -t 30 listener > /dev/null 2>&1
elapsed=$(( $(date +%s) - start ))
exit_code=$(docker inspect --format '{{.State.ExitCode}}' "$listener_before")
if [ "$exit_code" != 137 ] && [ "$elapsed" -lt 30 ]; then
    record signal:listener-sigterm ok "exit $exit_code after ${elapsed}s (137 would mean SIGKILL)"
else
    record signal:listener-sigterm FAIL "exit $exit_code after ${elapsed}s"
fi
# Age-based staleness is covered natively (tests/test_listener_health.py); this proves a stopped
# listener's last heartbeat cannot make any other container healthy.
if compose run --rm --no-deps -T listener python3 -m print_buddy.healthcheck > "$out/stopped-health.txt" 2>&1; then
    record heartbeat:after-stop FAIL "heartbeat of a stopped listener accepted"
else
    record heartbeat:after-stop ok "$(grep -m 1 FAIL "$out/stopped-health.txt")"
fi

spool=""
if [ "$mode" = bundled ]; then
    log "bundled Spoolman: create a spool"
    if compose up -d --wait --wait-timeout 180 spoolman > "$out/up-spoolman.txt" 2>&1 &&
        spool=$(compose exec -T spoolman python3 -c "
import json, urllib.request
def post(path, body):
    req = urllib.request.Request('http://127.0.0.1:8000/api/v1/' + path, json.dumps(body).encode(), {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=10))
f = post('filament', {'name': 'Acceptance PLA', 'material': 'PLA', 'density': 1.24, 'diameter': 1.75})
print(post('spool', {'filament_id': f['id'], 'location': 'IFS Slot 1'})['id'])"); then
        record spoolman:create ok "spool $spool"
    else
        record spoolman:create FAIL "see up-spoolman.txt"
    fi
fi

log "recreate every container (down without -v, up again)"
compose down > "$out/down-1.txt" 2>&1
compose up -d --wait --wait-timeout 180 fake-moonraker listener > "$out/up-2.txt" 2>&1 || record recreate:listener FAIL "see up-2.txt"
listener_after=$(compose ps -q listener)
if [ -n "$listener_after" ] && [ "$listener_after" != "$listener_before" ] &&
    compose exec -T listener sh -c 'grep -qx accepted /data/archive/acceptance-marker && grep -qx accepted /data/records/acceptance-marker'; then
    record recreate:data-persisted ok "new container ${listener_after:0:12}; markers intact"
else
    record recreate:data-persisted FAIL "container ${listener_after:-none}"
fi
if [ -n "$spool" ]; then
    compose up -d --wait --wait-timeout 180 spoolman > "$out/up-spoolman-2.txt" 2>&1 || record spoolman:recreate FAIL "see up-spoolman-2.txt"
    if compose exec -T spoolman python3 -c "
import json, sys, urllib.request
s = json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/spool/$spool', timeout=10))
sys.exit(0 if s['filament']['name'] == 'Acceptance PLA' and s['location'] == 'IFS Slot 1' else 1)"; then
        record spoolman:persisted ok "spool $spool survived recreation"
    else
        record spoolman:persisted FAIL "spool $spool missing after recreation"
    fi
fi

log "same-image tag switch (recreation coverage only, not a version upgrade)"
docker tag "$image_id" "pb-accept-tagswitch:$run_id"
sed -i "s#^PRINT_BUDDY_IMAGE=.*#PRINT_BUDDY_IMAGE=pb-accept-tagswitch:$run_id#" "$work/.env"
export PRINT_BUDDY_IMAGE="pb-accept-tagswitch:$run_id"
if compose up -d --wait --wait-timeout 180 fake-moonraker listener > "$out/up-3.txt" 2>&1 &&
    compose exec -T listener grep -qx accepted /data/archive/acceptance-marker; then
    record tagswitch:same-image ok "pb-accept-tagswitch:$run_id -> $image_id; data reused"
else
    record tagswitch:same-image FAIL "see up-3.txt"
fi

echo "results: $out/results.txt"
if [ "$failures" -gt 0 ]; then
    echo "FAILED: $failures check(s)" | tee -a "$out/results.txt"
    exit 1
fi
echo "PASSED: image $image_id ($mode)" | tee -a "$out/results.txt"
