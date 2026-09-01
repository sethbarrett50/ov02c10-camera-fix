#!/usr/bin/env bash
set -uo pipefail

echo "=== current default source ==="
pactl get-default-source

echo ""
echo "=== all sources with state ==="
pactl list short sources

echo ""
echo "=== is a bluetooth device connected right now? ==="
bluetoothctl devices Connected 2>&1
