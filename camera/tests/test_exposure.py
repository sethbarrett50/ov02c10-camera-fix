"""Tests for hardware exposure calibration — mocked subprocess/select, no hardware needed."""

import select
import subprocess

import numpy as np
import pytest

from ov02c10_camera import camera as camera_module
from ov02c10_camera.camera import V4L2Camera
from ov02c10_camera.config import CameraConfig


def test_calibrate_exposure_skips_adjustment_when_brightness_ok() -> None:
    """A frame already in the target range (90-170/255) needs no gain change."""
    cfg = CameraConfig()
    cam = V4L2Camera(cfg)
    bayer8 = np.full((cfg.sensor_height, cfg.sensor_width), 128, dtype=np.uint8)

    result = cam._calibrate_exposure(bayer8)

    assert result is bayer8


def test_calibrate_exposure_reduces_gain_when_too_bright(monkeypatch: pytest.MonkeyPatch) -> None:
    """A washed-out frame (e.g. 236/255, as seen live) should lower analogue_gain.

    This is the exact scenario found on real hardware: the fixed gain
    (tuned for a dark room) left channels at 90%+ of full range in bright
    conditions, clipping before white balance could help. See
    docs/DEBUGGING.md.
    """
    cfg = CameraConfig(analogue_gain=150)
    cam = V4L2Camera(cfg)
    cam.fd = 999  # unused: select() is mocked to never report ready

    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='/dev/v4l-subdev7', stderr='')

    monkeypatch.setattr(camera_module, 'find_sensor_entity', lambda media_device: 'ov02c10 5-0036')
    monkeypatch.setattr(camera_module, 'run_cmd', fake_run_cmd)
    monkeypatch.setattr(select, 'select', lambda *a, **k: ([], [], []))

    bayer8 = np.full((cfg.sensor_height, cfg.sensor_width), 236, dtype=np.uint8)
    result = cam._calibrate_exposure(bayer8)

    gain_calls = [c for c in calls if c[0] == 'v4l2-ctl']
    assert len(gain_calls) == 1
    # target=128, ratio=128/236≈0.542, new_gain=round(150*0.542)=81
    assert 'analogue_gain=81' in gain_calls[0][-1]
    # select() never reports ready, so no fresh frame was captured — original returned
    assert result is bayer8


def test_calibrate_exposure_clamps_to_valid_gain_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """New gain must stay within the sensor's documented 16-248 control range."""
    cfg = CameraConfig(analogue_gain=150)
    cam = V4L2Camera(cfg)
    cam.fd = 999

    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='/dev/v4l-subdev7', stderr='')

    monkeypatch.setattr(camera_module, 'find_sensor_entity', lambda media_device: 'ov02c10 5-0036')
    monkeypatch.setattr(camera_module, 'run_cmd', fake_run_cmd)
    monkeypatch.setattr(select, 'select', lambda *a, **k: ([], [], []))

    # Near-black frame -> would compute a huge gain multiplier without clamping
    bayer8 = np.full((cfg.sensor_height, cfg.sensor_width), 2, dtype=np.uint8)
    cam._calibrate_exposure(bayer8)

    gain_calls = [c for c in calls if c[0] == 'v4l2-ctl']
    assert 'analogue_gain=248' in gain_calls[0][-1]
