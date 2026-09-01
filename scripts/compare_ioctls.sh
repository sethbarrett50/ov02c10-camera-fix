#!/usr/bin/env bash
set -uo pipefail

echo "=== stracing cam (libcamera) ==="
strace -f -v -e trace=ioctl -o /tmp/cam_strace.log cam -c 1 --capture=3 -F >/dev/null 2>&1

echo "=== stracing our app ==="
cd /home/claude/dellxps/ov02c10-camera-fix/camera
strace -f -v -e trace=ioctl -o /tmp/ours_strace.log .venv/bin/ov02c10-camera >/dev/null 2>&1

echo ""
echo "=== unique ioctl request names: cam ==="
grep -oE "VIDIOC_[A-Z_]+" /tmp/cam_strace.log | sort -u

echo ""
echo "=== unique ioctl request names: ours ==="
grep -oE "VIDIOC_[A-Z_]+" /tmp/ours_strace.log | sort -u

echo ""
echo "=== in cam but NOT in ours ==="
comm -23 <(grep -oE "VIDIOC_[A-Z_]+" /tmp/cam_strace.log | sort -u) <(grep -oE "VIDIOC_[A-Z_]+" /tmp/ours_strace.log | sort -u)

echo ""
echo "=== last 15 ioctl lines before cam's STREAMON (full detail) ==="
grep -B15 "VIDIOC_STREAMON" /tmp/cam_strace.log | grep ioctl | tail -15

echo ""
echo "=== last 15 ioctl lines before our STREAMON (full detail) ==="
grep -B15 "VIDIOC_STREAMON" /tmp/ours_strace.log | grep ioctl | tail -15
