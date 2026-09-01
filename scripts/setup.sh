#!/usr/bin/env bash
# Bootstrap a fresh Debian/Ubuntu box to build and run this project.
#
# Installs system packages (GStreamer + PyGObject bindings, v4l-utils,
# v4l2loopback-dkms), loads the v4l2loopback virtual camera device, installs
# uv if missing, and syncs Python dependencies.
#
# Usage:
#   ./scripts/setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "==> Installing system packages (apt, needs sudo)..."
sudo apt-get update
sudo apt-get install -y \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    v4l-utils \
    v4l2loopback-dkms

echo "==> Loading v4l2loopback virtual camera device (video_nr=48)..."
if ! lsmod | grep -q '^v4l2loopback'; then
    sudo modprobe v4l2loopback video_nr=48 card_label="OV02C10 Camera" exclusive_caps=1
else
    echo "    v4l2loopback already loaded, skipping."
fi

if [[ ! -e /dev/video48 ]]; then
    echo "WARNING: /dev/video48 still doesn't exist after modprobe — check 'dmesg | tail' for errors." >&2
fi

echo "==> Installing uv (if missing)..."
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "    uv already installed ($(uv --version))."
fi

echo "==> Syncing Python dependencies..."
cd "$REPO_ROOT/camera"
uv sync --dev

echo ""
echo "Setup complete. Next steps:"
echo "  make run       # foreground preview, Ctrl+C to stop"
echo "  make install   # install as a systemd --user service"
echo "  make help      # see all available commands"
