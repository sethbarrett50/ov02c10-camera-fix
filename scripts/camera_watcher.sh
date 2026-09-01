#!/usr/bin/env bash
# Starts/stops the ov02c10-camera systemd --user service based on whether
# anything other than the service itself has /dev/video48 open. Meant to
# run as its own lightweight, always-on systemd --user service
# (ov02c10-camera-watcher.service) so the camera hardware/GStreamer
# pipeline itself only runs while something (Brave, Zoom, Discord, ...)
# actually has the virtual camera open — not continuously from login.
# See docs/DEBUGGING.md and #10.
#
# Polling rather than inotify: distinguishing "an external app just
# opened the device" from "the camera service we just started opened it
# as the producer" is unreliable from raw inotify open/close events alone.
# Polling fuser + comparing against the service's own MainPID sidesteps
# that ambiguity entirely, at the cost of being slightly less instant
# (bounded by POLL_INTERVAL) than a pure event-driven watcher.
set -uo pipefail

DEVICE=/dev/video48
SERVICE=ov02c10-camera
POLL_INTERVAL=3
STOP_AFTER_IDLE_POLLS=3

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

has_external_holder() {
    local pids our_pid pid
    pids=$(fuser "$DEVICE" 2>/dev/null)
    [ -z "$pids" ] && return 1

    our_pid=$(systemctl --user show -p MainPID --value "$SERVICE" 2>/dev/null)

    for pid in $pids; do
        if [ "$pid" != "$our_pid" ]; then
            return 0
        fi
    done
    return 1
}

log "Camera watcher started, polling $DEVICE every ${POLL_INTERVAL}s"

idle_polls=0
while true; do
    sleep "$POLL_INTERVAL"

    is_active=$(systemctl --user is-active "$SERVICE" 2>/dev/null || true)

    if has_external_holder; then
        idle_polls=0
        if [ "$is_active" != "active" ]; then
            log "External holder detected on $DEVICE, starting $SERVICE"
            systemctl --user start "$SERVICE"
        fi
    elif [ "$is_active" = "active" ]; then
        idle_polls=$((idle_polls + 1))
        if [ "$idle_polls" -ge "$STOP_AFTER_IDLE_POLLS" ]; then
            log "No external holder for $((idle_polls * POLL_INTERVAL))s, stopping $SERVICE"
            systemctl --user stop "$SERVICE"
            idle_polls=0
        fi
    fi
done
