"""One-off diagnostic: capture a raw frame and dump per-channel byte stats
before any debayering, to check the SGRBG10 unpack bit-alignment convention.
"""

import fcntl
import select

import numpy as np

from ov02c10_camera.camera import V4L2Camera
from ov02c10_camera.config import CameraConfig
from ov02c10_camera.logging_setup import configure_logging
from ov02c10_camera.media_pipeline import free_device
from ov02c10_camera.v4l2_types import (
    V4L2_BUF_TYPE_VIDEO_CAPTURE,
    V4L2_MEMORY_MMAP,
    VIDIOC_DQBUF,
    VIDIOC_QBUF,
    V4L2Buffer,
)

configure_logging(verbose=False)

cfg = CameraConfig()
free_device(cfg.capture_device)

cam = V4L2Camera(cfg)
cam.open()

raw = None
for i in range(5):
    r, _, _ = select.select([cam.fd], [], [], 2.0)
    if not r:
        continue
    buf = V4L2Buffer()
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
    buf.memory = V4L2_MEMORY_MMAP
    fcntl.ioctl(cam.fd, VIDIOC_DQBUF, buf)
    cam.buffers[buf.index].seek(0)
    candidate = np.frombuffer(cam.buffers[buf.index].read(buf.bytesused), dtype=np.uint8)
    fcntl.ioctl(cam.fd, VIDIOC_QBUF, buf)
    print(f'frame {i}: sequence={buf.sequence} bytesused={buf.bytesused}')
    if i >= 1:
        raw = candidate
        break

if raw is None:
    print('Never got a non-warmup frame')
else:
    raw16 = raw.view(np.uint16)
    print('raw16 min/max/mean:', raw16.min(), raw16.max(), float(raw16.mean()))

    h2, w2 = cam._h2, cam._w2
    stride_pixels = cam.stride // 2
    h, w = cfg.sensor_height, cfg.sensor_width
    rows = raw16[: h * stride_pixels].reshape(h, stride_pixels)
    bayer10 = rows[:, :w]
    b = bayer10[:h2, :w2].astype(np.float64)

    tl = b[0::2, 0::2]  # top-left of each 2x2 quad
    tr = b[0::2, 1::2]  # top-right
    bl = b[1::2, 0::2]  # bottom-left
    br = b[1::2, 1::2]  # bottom-right

    print(f'quad position means: TL={tl.mean():.1f} TR={tr.mean():.1f} BL={bl.mean():.1f} BR={br.mean():.1f}')
    print()
    print('Hypothesis A — current code (RGGB: R=TL, G=(TR+BL)/2, B=BR):')
    g_a = (tr + bl) / 2
    print(f'  R={tl.mean():.1f} G={g_a.mean():.1f} B={br.mean():.1f}')
    print()
    print('Hypothesis B — original (GRBG: G=(TL+BR)/2, R=TR, B=BL):')
    g_b = (tl + br) / 2
    print(f'  R={tr.mean():.1f} G={g_b.mean():.1f} B={bl.mean():.1f}')

cam.close()
