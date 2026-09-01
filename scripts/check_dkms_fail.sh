#!/usr/bin/env bash
echo "=== running kernel ==="
uname -r
echo "=== dkms status ==="
dkms status v4l2loopback
echo "=== build log tail ==="
sudo tail -80 /var/lib/dkms/v4l2loopback/0.15.0/build/make.log
