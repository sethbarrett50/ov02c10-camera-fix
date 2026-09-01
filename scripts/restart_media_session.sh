#!/usr/bin/env bash
set -uo pipefail

echo "=== current pipewire/wireplumber status ==="
systemctl --user status pipewire pipewire-pulse wireplumber --no-pager 2>&1 | head -40

echo ""
echo "=== restarting ==="
systemctl --user restart pipewire pipewire-pulse wireplumber

sleep 2
echo ""
echo "=== status after restart ==="
systemctl --user status pipewire pipewire-pulse wireplumber --no-pager 2>&1 | head -40

echo ""
echo "=== audio devices visible to the system now ==="
pactl list short sources 2>&1

echo ""
echo "=== video devices ==="
v4l2-ctl --list-devices 2>&1
