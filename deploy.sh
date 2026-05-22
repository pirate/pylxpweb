#!/usr/bin/env bash
# Deploy the solar dashboard to the Home Assistant addon.
#
# Steps:
#   1. Sync solar_dashboard.py + config.yaml into addon-solar_dashboard/
#   2. tar+ssh upload to /addons/solar_dashboard/ on the HA host
#   3. If config.yaml version changed: supervisor "reload" + "update"
#      Else: supervisor "rebuild" (picks up code changes only)
#   4. Poll supervisor /info until state=started and version matches
#
# Requirements:
#   - Local: ssh access to squash@hass (passwordless preferred)
#   - Remote: sudo permission on the HA SSH addon (we use it to read the
#     supervisor's SUPERVISOR_TOKEN out of supervisor PID's /proc/*/environ
#     because the SSH addon doesn't get the token in user env)
#
# Usage:
#   ./deploy.sh                # standard deploy
#   ./deploy.sh --files-only   # upload only, no rebuild (useful for iterating
#                                on text changes that don't need a restart;
#                                NOTE: the Dockerfile bakes solar_dashboard.py
#                                in at build time, so the running container
#                                will NOT see code changes without a rebuild)
#
# The Dockerfile detail above is why this script exists: a naive
# "ssh + restart" workflow does not work — you must trigger a rebuild
# (image rebake) or update (when version bumped). Took an hour to figure
# this out so the path is encoded here.

set -euo pipefail

HOST="squash@hass"
ADDON_SLUG="local_solar_dashboard"
ADDON_DIR="/addons/solar_dashboard"
LOCAL_SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
ADDON_LOCAL_DIR="$LOCAL_SRC_DIR/addon-solar_dashboard"

FILES_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --files-only) FILES_ONLY=1 ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

cd "$LOCAL_SRC_DIR"

# --- 1. sync source files into addon dir ---
echo "→ syncing solar_dashboard.py into addon-solar_dashboard/"
cp solar_dashboard.py "$ADDON_LOCAL_DIR/solar_dashboard.py"

LOCAL_VERSION=$(awk '/^version:/ {gsub(/"/,"",$2); print $2; exit}' "$ADDON_LOCAL_DIR/config.yaml")
echo "→ local config.yaml version: $LOCAL_VERSION"

# Quick syntax check on the Python file so we fail fast.
python3 -c "import ast,sys; ast.parse(open('solar_dashboard.py').read())" \
    || { echo "✗ solar_dashboard.py has syntax errors"; exit 1; }

# --- 2. upload to HA host ---
echo "→ uploading to $HOST:$ADDON_DIR"
cd "$ADDON_LOCAL_DIR"
tar -cf - solar_dashboard.py config.yaml \
    | ssh "$HOST" "cd $ADDON_DIR && tar -xf -"

if [[ $FILES_ONLY -eq 1 ]]; then
    echo "✓ files uploaded (--files-only, skipping rebuild)"
    exit 0
fi

# --- 3. resolve supervisor token + decide rebuild vs update ---
# The SUPERVISOR_TOKEN lives in the supervisor process's environment, not
# in the SSH addon shell. Grep for it across all PIDs (the supervisor is
# usually one of the lowest non-init PIDs).
TOKEN=$(ssh "$HOST" 'for p in $(sudo ls /proc/ | grep "^[0-9]"); do tok=$(sudo cat /proc/$p/environ 2>/dev/null | tr "\0" "\n" | grep "^SUPERVISOR_TOKEN=" | cut -d= -f2); if [ -n "$tok" ]; then echo "$tok"; break; fi; done')

if [[ -z "$TOKEN" ]]; then
    echo "✗ could not find SUPERVISOR_TOKEN on host; cannot rebuild" >&2
    exit 1
fi

# Need supervisor to re-read config.yaml from disk before it knows the new
# version. Without this, "Version changed, use Update instead Rebuild"
# fires inconsistently.
echo "→ reloading addon repo metadata"
ssh "$HOST" "curl -fsS -X POST -H 'Authorization: Bearer $TOKEN' http://supervisor/addons/reload >/dev/null"

# Get the installed version supervisor knows about post-reload.
INSTALLED_VERSION=$(ssh "$HOST" "curl -fsS -H 'Authorization: Bearer $TOKEN' http://supervisor/addons/$ADDON_SLUG/info" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["version"])')
echo "→ installed version: $INSTALLED_VERSION   local: $LOCAL_VERSION"

if [[ "$INSTALLED_VERSION" == "$LOCAL_VERSION" ]]; then
    echo "→ versions match → triggering rebuild (re-bakes image with new code)"
    ENDPOINT="rebuild"
else
    echo "→ version bumped → triggering update (installs new version)"
    ENDPOINT="update"
fi

ssh "$HOST" "curl -fsS -X POST -H 'Authorization: Bearer $TOKEN' http://supervisor/addons/$ADDON_SLUG/$ENDPOINT" >/dev/null

# --- 4. poll until it's running on the expected version ---
echo "→ waiting for addon to settle (max 90s)…"
for i in $(seq 1 18); do
    sleep 5
    STATUS=$(ssh "$HOST" "curl -fsS -H 'Authorization: Bearer $TOKEN' http://supervisor/addons/$ADDON_SLUG/info" 2>/dev/null \
        | python3 -c 'import sys,json; d=json.load(sys.stdin)["data"]; print(d["version"], d["state"])' \
        2>/dev/null || echo "? ?")
    V="${STATUS% *}"
    S="${STATUS#* }"
    echo "    [$((i*5))s] version=$V  state=$S"
    if [[ "$V" == "$LOCAL_VERSION" && "$S" == "started" ]]; then
        echo "✓ deploy complete: version=$V state=$S"
        # --- 5. tell the HAOSKiosk addon to hard-refresh its browser so we
        # see the new HTML/JS/CSS immediately. Without this, luakit serves
        # the cached page until its own browser_refresh timer fires
        # (currently configured to 10 min). REST API on 127.0.0.1:8080 is
        # exposed from the kiosk addon and reachable from the SSH addon.
        echo "→ asking HAOSKiosk to refresh browser"
        if ssh "$HOST" "curl -fsS -X POST http://127.0.0.1:8080/refresh_browser" >/dev/null 2>&1; then
            echo "✓ kiosk refreshed"
        else
            echo "  (kiosk refresh failed — not fatal; will refresh on its own timer)"
        fi
        exit 0
    fi
done

echo "✗ timed out waiting for addon to be 'started' on version $LOCAL_VERSION" >&2
exit 1
