#!/usr/bin/env bash
# Bootstrap a fresh Debian/Ubuntu box to build and run this project.
#
# Installs system packages (GStreamer + PyGObject bindings, v4l-utils),
# builds v4l2loopback from upstream source via DKMS (Debian's packaged
# v4l2loopback-dkms is often too old for current kernels — see
# docs/DEBUGGING.md), sets up device permissions, loads the virtual camera
# device, installs uv if missing, and syncs Python dependencies.
#
# Usage:
#   ./scripts/setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
V4L2LOOPBACK_VERSION=0.15.4

echo "==> Installing system packages (apt, needs sudo)..."
sudo apt-get update
sudo apt-get install -y \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    v4l-utils

echo "==> Checking v4l2loopback for the running kernel ($(uname -r))..."
# Debian's packaged v4l2loopback-dkms lags upstream kernel-compat fixes (e.g.
# the setup_timer -> timer_setup API removal) and can fail to build on newer
# kernels — see docs/DEBUGGING.md. Build a known-good version from upstream
# source via DKMS instead, so it still auto-rebuilds on kernel upgrades.
if dkms status v4l2loopback 2>/dev/null | grep -q "$(uname -r)"; then
    echo "    v4l2loopback already built for this kernel, skipping."
elif command -v dkms >/dev/null 2>&1 && modprobe -n v4l2loopback 2>/dev/null; then
    echo "    v4l2loopback module already available for this kernel, skipping build."
else
    sudo apt-get install -y dkms build-essential "linux-headers-$(uname -r)"
    sudo dkms remove "v4l2loopback/${V4L2LOOPBACK_VERSION}" --all 2>/dev/null || true
    sudo apt-get remove -y v4l2loopback-dkms 2>/dev/null || true

    TMP_SRC="$(mktemp -d)"
    git clone --branch "v${V4L2LOOPBACK_VERSION}" --depth 1 \
        https://github.com/umlaeute/v4l2loopback.git "$TMP_SRC/v4l2loopback-${V4L2LOOPBACK_VERSION}"
    sudo rm -rf "/usr/src/v4l2loopback-${V4L2LOOPBACK_VERSION}"
    sudo cp -r "$TMP_SRC/v4l2loopback-${V4L2LOOPBACK_VERSION}" "/usr/src/v4l2loopback-${V4L2LOOPBACK_VERSION}"
    rm -rf "$TMP_SRC"

    sudo dkms add -m v4l2loopback -v "$V4L2LOOPBACK_VERSION"
    sudo dkms build -m v4l2loopback -v "$V4L2LOOPBACK_VERSION"
    sudo dkms install -m v4l2loopback -v "$V4L2LOOPBACK_VERSION"
    sudo depmod -a
fi

echo "==> Setting up /dev/video* permissions (video group, not root-only)..."
if [[ ! -f /etc/udev/rules.d/99-v4l2loopback.rules ]]; then
    echo 'KERNEL=="video[0-9]*", SUBSYSTEM=="video4linux", GROUP="video", MODE="0660"' \
        | sudo tee /etc/udev/rules.d/99-v4l2loopback.rules >/dev/null
    sudo udevadm control --reload-rules
fi
if ! groups | grep -qw video; then
    sudo usermod -aG video "$USER"
    echo "    Added $USER to the video group — log out and back in for this to take effect."
fi

echo "==> Loading v4l2loopback virtual camera device (video_nr=48)..."
if ! lsmod | grep -q '^v4l2loopback'; then
    sudo modprobe v4l2loopback video_nr=48 card_label="OV02C10 Camera" exclusive_caps=1
    sudo udevadm trigger --subsystem-match=video4linux
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
