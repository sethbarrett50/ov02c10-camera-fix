"""V4L2 mmap camera interface: buffer allocation, streaming, and debayering."""

import ctypes
import fcntl
import logging
import mmap
import os
import select

import numpy as np

from .config import CameraConfig
from .media_pipeline import set_sensor_gain, setup_media_pipeline
from .v4l2_types import (
    V4L2_BUF_TYPE_VIDEO_CAPTURE,
    V4L2_MEMORY_MMAP,
    VIDIOC_DQBUF,
    VIDIOC_QBUF,
    VIDIOC_QUERYBUF,
    VIDIOC_REQBUFS,
    VIDIOC_S_FMT,
    VIDIOC_STREAMOFF,
    VIDIOC_STREAMON,
    V4L2_PIX_FMT_pgAA,
    V4L2Buffer,
    V4L2Format,
    V4L2RequestBuffers,
)

log = logging.getLogger(__name__)


class V4L2Camera:
    """V4L2 mmap camera interface for the OV02C10 sensor.

    Handles device open/close, buffer allocation, streaming, and per-frame
    pgAA unpacking and software debayering.
    """

    def __init__(self, cfg: CameraConfig) -> None:
        """Initialise the camera interface and pre-allocate all working buffers.

        All numpy arrays used during debayering are allocated once here and
        reused every frame to avoid per-frame heap allocation and GC pressure.

        Args:
            cfg: Camera configuration.
        """
        self.cfg = cfg
        self.fd = -1
        self.buffers: list[mmap.mmap] = []
        self.frame_size = 0
        self.stride = 0
        self._first_frame = True

        h, w = cfg.sensor_height, cfg.sensor_width
        oh, ow = cfg.output_height, cfg.output_width
        h2, w2 = (h // 2) * 2, (w // 2) * 2
        rh, rw = h2 // 2, w2 // 2

        self._h2, self._w2 = h2, w2
        # Index maps go straight from output resolution to half-res Bayer-channel
        # coordinates. Previously these mapped to full sensor resolution and the
        # per-frame code upsampled half-res channels to full res with np.repeat
        # before downsampling back to output res — six large temp-array allocations
        # per frame (~30MB) at 30fps, which pegged CPU and drove RSS into the GBs via
        # allocator arena growth. Gathering directly at output res skips that
        # round-trip entirely.
        self._y_idx_half = np.clip(np.linspace(0, h - 1, oh).astype(np.int32) // 2, 0, rh - 1)
        self._x_idx_half = np.clip(np.linspace(0, w - 1, ow).astype(np.int32) // 2, 0, rw - 1)

        # Pre-allocated working arrays — written in-place every frame
        self._R = np.zeros((rh, rw), dtype=np.uint16)
        self._G = np.zeros((rh, rw), dtype=np.uint16)
        self._B = np.zeros((rh, rw), dtype=np.uint16)
        self._out_r = np.zeros((oh, ow), dtype=np.uint16)
        self._out_g = np.zeros((oh, ow), dtype=np.uint16)
        self._out_b = np.zeros((oh, ow), dtype=np.uint16)
        self._out_buf = bytearray(oh * ow * 4)
        self._out_view = np.frombuffer(self._out_buf, dtype=np.uint8).reshape(oh, ow, 4)

    def open(self) -> None:
        """Open the capture device, allocate mmap buffers, and start streaming.

        Also re-runs media pipeline configuration with the device fd held open,
        which is required because the IPU6 resets link state when the fd closes.
        """
        self.fd = os.open(self.cfg.capture_device, os.O_RDWR | os.O_NONBLOCK)
        log.info('Opened %s (fd=%d)', self.cfg.capture_device, self.fd)

        set_sensor_gain(self.cfg)

        fmt = V4L2Format()
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        fmt.fmt.pix.width = self.cfg.sensor_width
        fmt.fmt.pix.height = self.cfg.sensor_height
        fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_pgAA
        fmt.fmt.pix.field = 1
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)
        self.frame_size = fmt.fmt.pix.sizeimage
        self.stride = fmt.fmt.pix.bytesperline
        log.info(
            'Format: %dx%d pgAA, stride=%d, frame_size=%d bytes',
            fmt.fmt.pix.width,
            fmt.fmt.pix.height,
            self.stride,
            self.frame_size,
        )

        req = V4L2RequestBuffers()
        req.count = self.cfg.num_buffers
        req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        req.memory = V4L2_MEMORY_MMAP
        fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)
        log.info('Allocated %d kernel buffers', req.count)

        for i in range(req.count):
            buf = V4L2Buffer()
            buf.index = i
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
            buf.memory = V4L2_MEMORY_MMAP
            fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, buf)
            mm = mmap.mmap(self.fd, buf.length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=buf.m.offset)
            self.buffers.append(mm)
            fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)
            log.debug('  buffer %d: length=%d offset=%d', i, buf.length, buf.m.offset)

        log.info('Re-configuring media pipeline with device held open...')
        setup_media_pipeline(self.cfg)

        buf_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE)
        fcntl.ioctl(self.fd, VIDIOC_STREAMON, buf_type)
        log.info('Streaming started')

    def _unpack_pgAA(self, raw: np.ndarray) -> np.ndarray:
        """Unpack a pgAA frame buffer to an 8-bit Bayer array.

        pgAA is the IPU6 packed 10-bit format: each group of 5 bytes encodes
        4 pixels as [P0_hi, P1_hi, P2_hi, P3_hi, lo_nibbles]. The high byte
        of each pixel is used directly as an 8-bit value. The row stride is
        padded to a 64-byte boundary and must be respected when reshaping.

        Args:
            raw: Flat uint8 array of the raw frame buffer as read from mmap.

        Returns:
            2-D uint8 array of shape (sensor_height, sensor_width) containing
            the 8-bit Bayer mosaic values.
        """
        h, w, stride = self.cfg.sensor_height, self.cfg.sensor_width, self.stride
        rows = raw[: h * stride].reshape(h, stride)
        groups_per_row = stride // 5
        pixels_per_row = groups_per_row * 4
        bayer8 = rows[:, : groups_per_row * 5].reshape(h, groups_per_row, 5)[:, :, :4]
        return bayer8.reshape(h, pixels_per_row)[:, :w].astype(np.uint8)

    def read_frame(self) -> memoryview | None:
        """Dequeue one V4L2 frame, debayer it, and return a BGRx memoryview.

        Blocks up to 2 seconds waiting for a frame. The first frame after
        streaming starts is discarded because the sensor produces an
        overexposed warmup frame.

        Debayering uses a 2x2 block average (box filter) which avoids
        interpolation artefacts. Each 2x2 Bayer quad maps to one half-res
        pixel, gathered directly to the configured output size (no full
        sensor-resolution intermediate). White balance gains correct the
        GRBG green bias under typical indoor lighting.

        Returns:
            Memoryview into a pre-allocated bytearray (output_height *
            output_width * 4 bytes, BGRx format), or None on timeout.
        """
        r, _, _ = select.select([self.fd], [], [], 2.0)
        if not r:
            log.warning('Timeout waiting for frame')
            return None

        buf = V4L2Buffer()
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        buf.memory = V4L2_MEMORY_MMAP
        fcntl.ioctl(self.fd, VIDIOC_DQBUF, buf)

        self.buffers[buf.index].seek(0)
        raw = np.frombuffer(self.buffers[buf.index].read(buf.bytesused), dtype=np.uint8)
        fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)

        if self._first_frame:
            self._first_frame = False
            return None

        bayer8 = self._unpack_pgAA(raw)
        result = self._debayer(bayer8)

        log.debug(
            'Frame %d: %d bytes -> %dx%d BGRx',
            buf.sequence,
            buf.bytesused,
            self.cfg.output_width,
            self.cfg.output_height,
        )
        return result

    def _debayer(self, bayer8: np.ndarray) -> memoryview:
        """Debayer an 8-bit Bayer mosaic into a gain-corrected BGRx frame.

        Pure numpy — touches no hardware, so it's directly unit-testable.
        Debayering uses a 2x2 block average (box filter) which avoids
        interpolation artefacts. Each 2x2 Bayer quad maps to one half-res
        pixel, gathered directly to the configured output size (no full
        sensor-resolution intermediate). White balance gains correct the
        GRBG green bias under typical indoor lighting.

        Args:
            bayer8: 2-D uint8 Bayer mosaic, shape (sensor_height, sensor_width),
                as produced by _unpack_pgAA.

        Returns:
            Memoryview into a pre-allocated bytearray (output_height *
            output_width * 4 bytes, BGRx format).
        """
        b = bayer8[: self._h2, : self._w2]

        # 2x2 block average debayer into pre-allocated half-res channel arrays
        np.add(b[0::2, 0::2], b[1::2, 1::2], out=self._G, casting='unsafe')
        self._G //= 2
        np.copyto(self._R, b[0::2, 1::2], casting='unsafe')
        np.copyto(self._B, b[1::2, 0::2], casting='unsafe')

        # Gather straight from half-res Bayer channels to output resolution (one
        # fancy-index copy per channel, no full-res intermediate) then apply
        # brightness + white balance in-place at output size.
        sr = np.uint16(5)
        sg = np.uint16(4)  # 5 * 0.9 rounded
        sb = np.uint16(5)  # 5 * 1.05 rounded
        np.multiply(self._R[np.ix_(self._y_idx_half, self._x_idx_half)], sr, out=self._out_r, casting='unsafe')
        np.multiply(self._G[np.ix_(self._y_idx_half, self._x_idx_half)], sg, out=self._out_g, casting='unsafe')
        np.multiply(self._B[np.ix_(self._y_idx_half, self._x_idx_half)], sb, out=self._out_b, casting='unsafe')
        np.clip(self._out_r, 0, 255, out=self._out_r)
        np.clip(self._out_g, 0, 255, out=self._out_g)
        np.clip(self._out_b, 0, 255, out=self._out_b)

        # Pack as BGRx into pre-allocated output buffer
        self._out_view[:, :, 0] = self._out_b
        self._out_view[:, :, 1] = self._out_g
        self._out_view[:, :, 2] = self._out_r
        self._out_view[:, :, 3] = 0

        return memoryview(self._out_buf)

    def close(self) -> None:
        """Stop streaming, unmap buffers, and close the device fd."""
        if self.fd >= 0:
            buf_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE)
            try:
                fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, buf_type)
            except OSError:
                # Streaming may never have started (e.g. open() failed before
                # STREAMON), in which case STREAMOFF legitimately errors.
                log.debug('STREAMOFF failed on fd=%d (stream likely never started)', self.fd)
            for mm in self.buffers:
                mm.close()
            os.close(self.fd)
            self.fd = -1
            log.info('Camera closed')
