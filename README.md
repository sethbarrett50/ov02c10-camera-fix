# ov02c10-camera-fix

Working camera capture for laptops with an **OV02C10** sensor behind an
**Intel IPU6** image processor (e.g. Dell XPS 16) on Debian/mainline-kernel
systems, where neither `libcamera` nor Intel's proprietary camera HAL
produce a usable image out of the box.

Feeds a corrected image into a [v4l2loopback](https://github.com/umlaeute/v4l2loopback)
virtual camera device, so it shows up as a normal webcam in Brave, Chrome,
Teams, Zoom, Discord, etc.

## Why this exists

- `libcamera`'s software fallback IPA (`uncalibrated.yaml`) captures a
  valid first frame, then every frame after it comes out solid black —
  no real auto-gain control for this sensor.
- Intel's proprietary HAL (`icamerasrc`/`libcamhal`), even if installed
  with real tuning data for this exact sensor, fails with
  `Failed to open PSYS` — the IPU6's hardware ISP driver isn't in
  mainline/Debian kernels, only Ubuntu's OEM-patched kernel or an
  out-of-tree DKMS module.

See [`docs/DEBUGGING.md`](docs/DEBUGGING.md) for the full investigation,
including diagnostic commands for confirming you're hitting the same
issues on your own hardware.

This repo instead captures raw frames directly via V4L2, debayers and
gain-corrects them in software (numpy), and streams the result into a
v4l2loopback device via GStreamer — no dependency on either broken path.

**Known limitation:** there's no real auto-exposure loop. Gain is a fixed,
tuned value (see `--analogue-gain`/`--digital-gain`) — good enough for a
webcam in a stable-lighting environment, not a general fix for varying
light. See [open issues](../../issues) for planned improvements.

## Requirements

- OV02C10 sensor behind Intel IPU6 (`intel_ipu6` + `intel_ipu6_isys`
  kernel modules loaded — check with `lsmod | grep ipu6`)
- `v4l2loopback-dkms` (for the virtual camera device)
- `gstreamer1.0-tools`, `gstreamer1.0-plugins-base`,
  `python3-gi`, `gir1.2-gst-1.0` (GStreamer + Python bindings — installed
  from your distro's package manager, not pip)
- `v4l-utils` (`media-ctl`, `v4l2-ctl`)
- [`uv`](https://docs.astral.sh/uv/) for Python dependency management

Run `make help` for shortcuts covering everything below (`make sync`,
`make run`, `make install`, `make gain`, `make logs`, etc.).

## Setup

```bash
# 1. Load the v4l2loopback virtual camera device
sudo modprobe v4l2loopback video_nr=48 card_label="OV02C10 Camera" exclusive_caps=1

# 2. Install Python deps
cd camera
uv sync

# 3. Confirm the sensor's current media entity name (bus number can change per boot)
media-ctl -d /dev/media0 -p | grep -i ov02c10

# 4. Test run in the foreground first (opens a preview window)
uv run main.py

# 5. Once it looks right, install as a systemd --user service for loopback mode
mkdir -p ~/code/ov02c10-camera-fix
cp -r ../camera ~/code/ov02c10-camera-fix/
mkdir -p ~/.config/systemd/user
cp ../systemd/ov02c10-camera.service ~/.config/systemd/user/
mkdir -p ~/.config/systemd/user/ov02c10-camera.service.d
cp ../systemd/override.conf ~/.config/systemd/user/ov02c10-camera.service.d/
systemctl --user daemon-reload
systemctl --user enable --now ov02c10-camera
```

The `override.conf` caps the service at 1GB RAM / 1.5 CPU cores as a
safety net — if a future regression leaks resources again, systemd kills
and restarts it instead of it taking down your machine.

## Tuning for your environment

Gain values in `camera/main.py`'s `CameraConfig` (`analogue_gain=150`,
`digital_gain=4096`) were tuned for one indoor room and are not adaptive.
If your image is too dark or too bright, check current sensor state and
retune:

```bash
v4l2-ctl -d "$(media-ctl -d /dev/media0 -e 'ov02c10 <BUS>-0036')" -l
uv run main.py --analogue-gain 100 --digital-gain 2048   # example
```

(`<BUS>` is whatever `media-ctl -p | grep ov02c10` reports for your boot —
see [`docs/DEBUGGING.md`](docs/DEBUGGING.md).)

## License

MIT — see [LICENSE](LICENSE).
