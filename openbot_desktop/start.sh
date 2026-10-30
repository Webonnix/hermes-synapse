#!/bin/bash
# Runs the Hermes pieces beside the upstream OpenBot entrypoint, then hands the
# process over to it unchanged (D-Bus, keyring, the two Xvfb displays, OpenBot).
#
# Every helper starts in a subshell that exits at once, so tini adopts it — the
# same trick the upstream script uses for Xvfb: as children of OpenBot they
# would inherit the agents' high OOM score and die with them.
set -u

# The bridge first: /health answers while the displays are still coming up.
(/opt/hermes-bridge/bin/uvicorn server:app --app-dir /desktop --host 0.0.0.0 --port 8900 --no-access-log &)

screens() {
    for display in 0 1; do
        for _ in $(seq 1 600); do
            [ -e "/tmp/.X11-unix/X$display" ] && break
            sleep 0.1
        done
    done
    # Loopback only and no VNC password: the bridge's /vnc endpoint is the one
    # way in, and it requires the desktop's bearer token. -noshm: no MIT-SHM
    # under gVisor. Screen 0 is OpenBot itself, screen 1 the agents' desktop.
    x11vnc -display :0 -rfbport 5900 -localhost -forever -shared -noshm -nopw -quiet >/dev/null 2>&1 &
    x11vnc -display :1 -rfbport 5901 -localhost -forever -shared -noshm -nopw -quiet >/dev/null 2>&1 &
    # The owner's terminal for `openbot login` and `openbot mobile-connect`, on
    # OpenBot's own screen so the agents' screenshots never show it. Kept alive.
    while true; do
        DISPLAY=:0 xterm -geometry 110x28+40+40 -title "OpenBot terminal" -e bash -l >/dev/null 2>&1
        sleep 2
    done
}
(screens &)

exec /opt/OpenBot/app/resources/hosting/openbot-container
