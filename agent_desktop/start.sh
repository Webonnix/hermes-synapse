#!/bin/bash
# Brings up the visible desktop, then hands the process over to the bridge.
# The desktop pieces run in the background: if one of them dies, /health
# reports it (display/vnc false) instead of the whole container exiting while
# an agent is mid-task.
set -u

mkdir -p "$AGENT_WORKSPACE" "$HOME/.config/chromium-agent"

Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp &

for _ in $(seq 1 50); do
    [ -e "/tmp/.X11-unix/X${DISPLAY#:}" ] && break
    sleep 0.1
done

fluxbox >/dev/null 2>&1 &

# Loopback only and no VNC password: the bridge's /vnc endpoint is the one way
# in, and it requires the desktop's bearer token. -noshm because MIT-SHM is not
# available under gVisor.
x11vnc -display "$DISPLAY" -rfbport 5900 -localhost -forever -shared -noshm -nopw -quiet >/dev/null 2>&1 &

# The agent's own browser, on screen so the owner sees what it does. Chromium's
# own sandbox needs kernel features gVisor does not offer; the container is the
# sandbox here. CDP stays on loopback for agents that drive the browser.
chromium --no-sandbox --no-first-run --no-default-browser-check --disable-dev-shm-usage \
    --user-data-dir="$HOME/.config/chromium-agent" \
    --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
    --window-position=0,0 --window-size=1440,860 about:blank >/dev/null 2>&1 &

# A terminal for the owner: signing a CLI agent in (`grok login`) happens here.
xterm -geometry 120x30+80+80 -e bash -l >/dev/null 2>&1 &

exec uvicorn server:app --app-dir /desktop --host 0.0.0.0 --port 8900 --no-access-log
