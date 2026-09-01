#!/usr/bin/env bash
set -euo pipefail

VERSION=0.15.4

echo "==> Removing broken v4l2loopback-dkms 0.15.0..."
sudo dkms remove v4l2loopback/0.15.0 --all 2>/dev/null || true
sudo apt-get remove -y v4l2loopback-dkms

echo "==> Fetching v4l2loopback ${VERSION} source..."
rm -rf "/tmp/v4l2loopback-${VERSION}"
git clone --branch "v${VERSION}" --depth 1 https://github.com/umlaeute/v4l2loopback.git "/tmp/v4l2loopback-${VERSION}"
sudo cp -r "/tmp/v4l2loopback-${VERSION}" "/usr/src/v4l2loopback-${VERSION}"

echo "==> Registering with DKMS and building for the running kernel..."
sudo dkms add -m v4l2loopback -v "${VERSION}"
sudo dkms build -m v4l2loopback -v "${VERSION}"
sudo dkms install -m v4l2loopback -v "${VERSION}"
sudo depmod -a

echo "==> Loading the module..."
sudo modprobe v4l2loopback video_nr=48 card_label="OV02C10 Camera" exclusive_caps=1

echo "==> Done. Checking /dev/video48..."
ls -la /dev/video48
