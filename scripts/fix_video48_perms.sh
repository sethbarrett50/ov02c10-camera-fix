#!/usr/bin/env bash
set -euo pipefail

echo "=== are you in the video group? ==="
groups

echo "=== unblock right now (temporary, lost on reload/reboot) ==="
sudo chgrp video /dev/video48
sudo chmod 660 /dev/video48
ls -la /dev/video48

echo "=== make it permanent: udev rule for future module loads ==="
echo 'KERNEL=="video[0-9]*", SUBSYSTEM=="video4linux", GROUP="video", MODE="0660"' \
  | sudo tee /etc/udev/rules.d/99-v4l2loopback.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=video4linux

echo "=== ensure your user is in the video group (needed for reads/writes) ==="
if ! groups | grep -qw video; then
    sudo usermod -aG video "$USER"
    echo "Added $USER to video group — you MUST log out and back in for this to take effect."
else
    echo "$USER already in video group."
fi
