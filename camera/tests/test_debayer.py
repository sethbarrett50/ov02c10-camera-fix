"""Tests for the debayer + white-balance math — pure numpy, no hardware needed.

Regression coverage for the CPU/memory blowup fix: the debayer step used
to upsample half-res channels to full sensor resolution with np.repeat
before downsampling back down, allocating ~30MB/frame and pegging a CPU
core. These tests pin down that the direct half-res -> output-res gather
produces identical numeric results, plus the output shape/dtype contract
gst_pipeline.py relies on.
"""

import numpy as np

from ov02c10_camera.camera import V4L2Camera
from ov02c10_camera.config import CameraConfig


def _solid_color_bayer(cfg: CameraConfig, r: int, g: int, b: int) -> np.ndarray:
    """Build a full-size Bayer mosaic where every 2x2 quad is the same R/G/B.

    Matches the GRBG pattern V4L2Camera._debayer assumes: (0,0)/(1,1)=G,
    (0,1)=R, (1,0)=B. Confirmed against real hardware by directly comparing
    per-quad-position channel means from a live capture — see
    docs/DEBUGGING.md.

    Args:
        cfg: Camera config supplying sensor dimensions.
        r: Red channel value to fill (0-255).
        g: Green channel value to fill (0-255).
        b: Blue channel value to fill (0-255).

    Returns:
        2-D uint8 array of shape (sensor_height, sensor_width).
    """
    bayer8 = np.zeros((cfg.sensor_height, cfg.sensor_width), dtype=np.uint8)
    bayer8[0::2, 0::2] = g
    bayer8[1::2, 1::2] = g
    bayer8[0::2, 1::2] = r
    bayer8[1::2, 0::2] = b
    return bayer8


def test_debayer_defaults_to_identity_gain_when_uncalibrated() -> None:
    """Before white-balance calibration runs, gains are identity (64/64 = 1.0x).

    _debayer() itself never picks gain values — only _calibrate_white_balance()
    does, and only real streamed frames trigger it via read_frame(). Calling
    _debayer() directly (as these tests do) never calibrates, so output should
    exactly match input.
    """
    cfg = CameraConfig()
    cam = V4L2Camera(cfg)
    bayer8 = _solid_color_bayer(cfg, r=60, g=200, b=20)

    result = cam._debayer(bayer8)

    assert isinstance(result, memoryview)
    assert len(result) == cfg.output_height * cfg.output_width * 4
    out = np.frombuffer(result, dtype=np.uint8).reshape(cfg.output_height, cfg.output_width, 4)
    # BGRx channel order
    assert np.unique(out[:, :, 0]).tolist() == [20]  # B
    assert np.unique(out[:, :, 1]).tolist() == [200]  # G
    assert np.unique(out[:, :, 2]).tolist() == [60]  # R
    assert np.unique(out[:, :, 3]).tolist() == [0]  # x/alpha padding


def test_calibrate_white_balance_equalizes_channels_to_green() -> None:
    """Gray-world calibration scales R/B gain (6-bit fixed point) to match G's mean.

    A flat/unstable software gain missed badly once room lighting changed
    between test runs (raw frame mean moved from ~437 to ~915 out of 1023
    within minutes) — see docs/DEBUGGING.md. Calibrating from the actual
    first frame of each session tracks whatever lighting is really present.
    """
    cfg = CameraConfig()
    cam = V4L2Camera(cfg)
    # R mean 100, G mean 150, B mean 75 -> gains should scale R and B up to
    # match G: sr = round(150/100*64) = 96, sb = round(150/75*64) = 128.
    bayer8 = _solid_color_bayer(cfg, r=100, g=150, b=75)

    cam._calibrate_white_balance(bayer8)

    assert cam._wb_calibrated is True
    assert int(cam._sr) == 96
    assert int(cam._sg) == 64
    assert int(cam._sb) == 128


def test_calibrate_white_balance_clamps_extreme_ratios() -> None:
    """A near-black or near-saturated channel shouldn't produce a huge/tiny gain."""
    cfg = CameraConfig()
    cam = V4L2Camera(cfg)
    # R mean 1 (near-black) -> ratio would be huge without clamping
    bayer8 = _solid_color_bayer(cfg, r=1, g=200, b=200)

    cam._calibrate_white_balance(bayer8)

    # Clamped to at most 3.0x -> round(3.0 * 64) = 192
    assert int(cam._sr) == 192


def test_calibrate_white_balance_excludes_clipped_regions() -> None:
    """A blown-out region (e.g. a bright face) shouldn't skew the gray-world ratio.

    Regression test for #16: whole-frame gray-world calibration was biased
    by face-dominated framing, producing a bright/white face with a
    green-tinted background.
    """
    cfg = CameraConfig()
    cam = V4L2Camera(cfg)
    bayer8 = _solid_color_bayer(cfg, r=100, g=150, b=75)
    # Overwrite the top quarter of the frame with a fully clipped region —
    # simulates a blown-out face filling part of the shot.
    quarter_h = (cfg.sensor_height // 4) // 2 * 2
    bayer8[:quarter_h, :] = 255

    cam._calibrate_white_balance(bayer8)

    # Should match the un-clipped region's ratios exactly, unaffected by
    # the clipped region — same values as the fully-neutral-frame test.
    assert int(cam._sr) == 96
    assert int(cam._sb) == 128


def test_debayer_output_shape_matches_configured_output_resolution() -> None:
    """Output must match output_width/output_height regardless of sensor size."""
    cfg = CameraConfig(output_width=320, output_height=240)
    cam = V4L2Camera(cfg)
    bayer8 = _solid_color_bayer(cfg, r=10, g=10, b=10)

    result = cam._debayer(bayer8)

    assert len(result) == 240 * 320 * 4


def test_debayer_reuses_preallocated_buffer_across_calls() -> None:
    """Pre-allocated output buffer is written in place, not reallocated per frame.

    This is what makes the fix work: no per-frame heap allocation in the hot
    path. Calling twice must return a view into the *same* underlying object.
    """
    cfg = CameraConfig()
    cam = V4L2Camera(cfg)
    bayer8 = _solid_color_bayer(cfg, r=60, g=40, b=20)

    first = cam._debayer(bayer8)
    second = cam._debayer(bayer8)

    assert first.obj is cam._out_buf
    assert second.obj is cam._out_buf
