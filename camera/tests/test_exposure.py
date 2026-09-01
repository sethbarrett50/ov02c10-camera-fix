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


def test_calibrate_exposure_reduces_gain_when_a_subregion_stays_clipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bright subregion (e.g. a face) can stay clipped while the whole-frame average looks fine.

    Regression test for #16 — reported live as a face that looked "like
    headlights" despite the calibration log showing reasonable numbers.
    Those numbers come from _calibrate_white_balance(), which correctly
    excludes the clipped region from its color-ratio math — but the
    original exposure check only looked at the whole-frame average, which
    a dim-enough background can pull back into the "OK" range even while
    ~20% of the frame is fully saturated.
    """
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

    h2 = cam._h2
    bayer8 = np.full((cfg.sensor_height, cfg.sensor_width), 99, dtype=np.uint8)
    clipped_rows = int(h2 * 0.2)
    clipped_rows -= clipped_rows % 2  # keep even for 2x2 quad alignment
    bayer8[:clipped_rows, :] = 255

    # Sanity check: whole-frame average lands in the "OK" 90-170 range
    # despite ~20% of the frame being fully saturated.
    assert 90.0 <= bayer8[: cam._h2, : cam._w2].mean() <= 170.0

    cam._calibrate_exposure(bayer8)

    gain_calls = [c for c in calls if c[0] == 'v4l2-ctl']
    # The brightness-average pass should NOT fire (average looks fine) —
    # only the clipped-fraction pass should.
    assert len(gain_calls) == 1
    assert 'analogue_gain=105' in gain_calls[0][-1]  # round(150 * 0.7)


def test_calibrate_exposure_iterates_and_stops_once_clipping_resolves() -> None:
    """Keeps reducing gain across multiple passes if one step isn't enough, then stops.

    Regression test for #16: a single fixed 0.7x follow-up step wasn't
    always enough — 75% of the frame was still observed clipped live
    after exactly one reduction. Tests the loop directly via
    _apply_gain_and_settle (the hardware-facing boundary) rather than
    faking the whole V4L2 ioctl chain.
    """
    cfg = CameraConfig(analogue_gain=150)
    cam = V4L2Camera(cfg)

    h2 = cam._h2

    def clipped_frame(fraction: float) -> np.ndarray:
        rows = int(h2 * fraction)
        rows -= rows % 2
        frame = np.full((cfg.sensor_height, cfg.sensor_width), 20, dtype=np.uint8)
        frame[:rows, :] = 255
        return frame

    # 60% clipped, weighted average (~161/255) still lands in the "OK"
    # 90-170 range, so the brightness-average pass doesn't fire — only
    # the clipped-fraction loop should.
    initial = clipped_frame(0.6)
    frame_after_first_reduction = clipped_frame(0.2)  # still >10%, needs another pass
    frame_after_second_reduction = clipped_frame(0.05)  # <=10%, loop should stop here

    calls: list[int] = []

    def fake_apply_gain_and_settle(new_gain: int) -> np.ndarray:
        calls.append(new_gain)
        return frame_after_first_reduction if len(calls) == 1 else frame_after_second_reduction

    cam._apply_gain_and_settle = fake_apply_gain_and_settle  # type: ignore[method-assign]

    result = cam._calibrate_exposure(initial)

    # 150 -> 75 (60% clipped, >50% -> 0.5x step) -> 52 (20% clipped, <=50% -> 0.7x step)
    assert calls == [75, 52]
    assert np.array_equal(result, frame_after_second_reduction)


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
