"""Tests for unpacked-10-bit raw buffer unpacking — pure numpy, no hardware needed."""

import numpy as np

from ov02c10_camera.camera import V4L2Camera
from ov02c10_camera.config import CameraConfig


def _tiny_camera() -> V4L2Camera:
    """Build a V4L2Camera against a tiny synthetic sensor size for fast tests.

    V4L2Camera.__init__ only allocates numpy arrays — it never touches
    hardware, so this is safe to construct without a real device. `stride`
    is normally set by open()'s VIDIOC_S_FMT ioctl; tests set it directly.

    Returns:
        A V4L2Camera configured for an 8x2 sensor with no row padding
        (V4L2_PIX_FMT_SGRBG10 is 2 bytes/pixel, so 8 pixels = 16 bytes/row).
    """
    cfg = CameraConfig(sensor_width=8, sensor_height=2, output_width=8, output_height=2)
    cam = V4L2Camera(cfg)
    cam.stride = 16
    return cam


def test_unpack_sgrbg10_shifts_10_bit_samples_to_8_bit() -> None:
    """Each pixel is a 16-bit LE value with the 10-bit sample in the low bits.

    Right-shifting by 2 recovers the top 8 bits, matching the approximation
    used by the previous packed-format unpacking.
    """
    cam = _tiny_camera()
    row0 = [0, 100, 200, 300, 400, 500, 600, 700]
    row1 = [50, 150, 250, 350, 450, 550, 650, 750]
    raw = np.array(row0 + row1, dtype='<u2').view(np.uint8)

    bayer8 = cam._unpack_sgrbg10(raw)

    assert bayer8.shape == (2, 8)
    assert bayer8[0].tolist() == [v >> 2 for v in row0]
    assert bayer8[1].tolist() == [v >> 2 for v in row1]
    assert bayer8.dtype == np.uint8
