"""Tests for the debayer + gain-correction math — pure numpy, no hardware needed.

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

    Matches the SGRBG pattern V4L2Camera._debayer assumes: (0,0)/(1,1)=G,
    (0,1)=R, (1,0)=B.

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


def test_debayer_applies_known_gain_and_clips() -> None:
    """R*5, G*4, B*5 gains, clipped to 255 — the exact values tuned on hardware.

    See docs/DEBUGGING.md for how these were picked (gray-room test).
    """
    cfg = CameraConfig()
    cam = V4L2Camera(cfg)
    bayer8 = _solid_color_bayer(cfg, r=60, g=40, b=20)

    result = cam._debayer(bayer8)

    assert isinstance(result, memoryview)
    assert len(result) == cfg.output_height * cfg.output_width * 4
    out = np.frombuffer(result, dtype=np.uint8).reshape(cfg.output_height, cfg.output_width, 4)
    # BGRx channel order
    assert np.unique(out[:, :, 0]).tolist() == [100]  # B: 20*5
    assert np.unique(out[:, :, 1]).tolist() == [160]  # G: 40*4
    assert np.unique(out[:, :, 2]).tolist() == [255]  # R: 60*5 clipped from 300
    assert np.unique(out[:, :, 3]).tolist() == [0]  # x/alpha padding


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
