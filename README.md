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
  `python3-gi`, `gir1.2-gstreamer-1.0` (GStreamer + Python bindings —
  installed from your distro's package manager, not pip)
- `v4l-utils` (`media-ctl`, `v4l2-ctl`)
- [`uv`](https://docs.astral.sh/uv/) for Python dependency management

All of the above (except the kernel modules — see below) are installed
automatically by `make setup` / `scripts/setup.sh`.

## Setup

```bash
# 1. One-shot bootstrap: apt packages, v4l2loopback device, uv, Python deps
make setup

# 2. Confirm the sensor's current media entity name (bus number can change per boot)
media-ctl -d /dev/media0 -p | grep -i ov02c10

# 3. Test run in the foreground first (opens a preview window)
make run

# 4. For browser/Teams/Zoom use, feed the loopback device in the foreground
make run-loopback
```

`make run-loopback` is currently the supported way to use this as a
webcam — leave it running in a terminal while you're on a call. There is
also an on-demand systemd setup (`make install`, below) that tries to
start/stop the pipeline automatically, but it **doesn't work with
Chrome/Brave** — see the "Known issue" section in
[`docs/DEBUGGING.md`](docs/DEBUGGING.md) for why.

`make setup` needs `sudo` for package installation and loading the
`v4l2loopback` kernel module — it will prompt. It's idempotent, safe to
re-run.

`make install` copies `camera/` to `~/code/ov02c10-camera-fix/camera`,
syncs deps there, and installs two systemd `--user` units + a resource-cap
override from `systemd/`:

- `ov02c10-camera-watcher.service` — a lightweight always-on watcher
  (enabled to start at login) that polls whether anything actually has
  `/dev/video48` open (Brave, Zoom, Discord, ...)
- `ov02c10-camera.service` — the actual camera/GStreamer pipeline, started
  and stopped on-demand *by the watcher*, not enabled for auto-start
  itself. Running the camera hardware continuously from login was wasted
  CPU/power for a webcam that's only used occasionally.

So after `make install`, nothing shows video until an app actually opens
the loopback device — the camera light/pipeline turns on within a few
seconds of opening Brave's camera picker (or similar) and turns off a few
seconds after the last consumer closes it. `make logs`/`make logs-watcher`
tail each service's journal if you want to watch this happen.

**This doesn't currently work for browser cameras** (Chrome/Brave never
lists the device in the first place, so nothing ever triggers the
watcher) — see [`docs/DEBUGGING.md`](docs/DEBUGGING.md). Use
`make run-loopback` instead for now.

The `override.conf` caps the camera service at 1GB RAM / 1.5 CPU cores as
a safety net — if a future regression leaks resources again, systemd
kills and restarts it instead of it taking down your machine.

Run `make help` to see every available command (`make test`, `make logs`,
`make gain`, `make lint`, etc.).

## Development

```
camera/
  pyproject.toml
  ov02c10_camera/
    cli.py             # argument parsing, entry point
    config.py          # CameraConfig dataclass
    logging_setup.py   # logging configuration
    media_pipeline.py  # media-ctl/v4l2-ctl wiring (sensor discovery, gain, links)
    camera.py          # V4L2Camera: mmap capture + debayer
    gst_pipeline.py     # GStreamer sink wiring (preview window or v4l2loopback)
  tests/                # pytest — debayer math, pgAA unpacking, media-ctl parsing
```

`make test` runs the test suite (`camera/tests/`), which covers the
hardware-independent logic (debayer math, pgAA unpacking, sensor-entity
regex parsing against mocked `media-ctl` output) — it can't exercise the
actual V4L2/GStreamer hardware path, which only real hardware can test.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Tuning for your environment

Gain values in `CameraConfig` (`analogue_gain=150`, `digital_gain=4096`)
were tuned for one indoor room and are not adaptive. If your image is too
dark or too bright, check current sensor state and retune:

```bash
make gain   # prints current exposure/gain control values
cd camera && uv run ov02c10-camera --analogue-gain 100 --digital-gain 2048   # example
```

See [`docs/DEBUGGING.md`](docs/DEBUGGING.md) for how the defaults were
picked.

## License

MIT — see [LICENSE](LICENSE).
