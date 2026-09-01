"""Tests for pgAA raw-buffer unpacking — pure numpy, no hardware needed."""

import numpy as np

from ov02c10_camera.camera import V4L2Camera
from ov02c10_camera.config import CameraConfig


def _tiny_camera() -> V4L2Camera:
    """Build a V4L2Camera against a tiny synthetic sensor size for fast tests.

    V4L2Camera.__init__ only allocates numpy arrays — it never touches
    hardware, so this is safe to construct without a real device. `stride`
    is normally set by open()'s VIDIOC_S_FMT ioctl; tests set it directly.

    Returns:
        A V4L2Camera configured for an 8x2 sensor with no row padding.
    """
    cfg = CameraConfig(sensor_width=8, sensor_height=2, output_width=8, output_height=2)
    cam = V4L2Camera(cfg)
    cam.stride = 10  # 2 groups of 5 bytes/row = 8 pixels/row, no padding
    return cam


def test_unpack_pgAA_extracts_high_byte_per_pixel() -> None:
    """Each group of 5 bytes -> 4 pixels, using the first 4 bytes directly.

    The 5th byte (packed low 2 bits of each 10-bit pixel) is discarded —
    _unpack_pgAA only recovers the 8 MSBs, which is the documented
    approximation this whole pipeline relies on.
    """
    cam = _tiny_camera()
    row0 = [0, 10, 20, 30, 0, 40, 50, 60, 70, 0]  # 2 groups, low-nibble byte ignored
    row1 = [1, 11, 21, 31, 0, 41, 51, 61, 71, 0]
    raw = np.array(row0 + row1, dtype=np.uint8)

    bayer8 = cam._unpack_pgAA(raw)

    assert bayer8.shape == (2, 8)
    assert bayer8[0].tolist() == [0, 10, 20, 30, 40, 50, 60, 70]
    assert bayer8[1].tolist() == [1, 11, 21, 31, 41, 51, 61, 71]
    assert bayer8.dtype == np.uint8
