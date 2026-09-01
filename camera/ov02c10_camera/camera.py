"""V4L2 mmap camera interface: buffer allocation, streaming, and debayering."""

import ctypes
import fcntl
import logging
import mmap
import os
import select

import numpy as np

from .config import CameraConfig
from .media_pipeline import find_sensor_entity, run_cmd, set_sensor_gain, setup_media_pipeline
from .v4l2_types import (
    V4L2_BUF_TYPE_VIDEO_CAPTURE,
    V4L2_MEMORY_MMAP,
    V4L2_PIX_FMT_SGRBG10,
    VIDIOC_DQBUF,
    VIDIOC_QBUF,
    VIDIOC_QUERYBUF,
    VIDIOC_REQBUFS,
    VIDIOC_S_FMT,
    VIDIOC_STREAMOFF,
    VIDIOC_STREAMON,
    V4L2Buffer,
    V4L2Format,
    V4L2RequestBuffers,
)

log = logging.getLogger(__name__)


class V4L2Camera:
    """V4L2 mmap camera interface for the OV02C10 sensor.

    Handles device open/close, buffer allocation, streaming, and per-frame
    raw-buffer unpacking and software debayering.
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

        # 6-bit fixed-point white-balance gains (>>6 = /64), 64 = identity.
        # Calibrated from the first real frame of each session in
        # _calibrate_white_balance() rather than hardcoded — a fixed
        # constant tuned for one lighting snapshot missed badly once room
        # lighting changed between test runs (raw frame mean moved from
        # ~437 to ~915 out of 1023 between two captures minutes apart).
        # See docs/DEBUGGING.md and #3 (real per-frame auto-gain is a
        # further step beyond this one-shot-per-session calibration).
        self._sr = np.uint16(64)
        self._sg = np.uint16(64)
        self._sb = np.uint16(64)
        self._wb_calibrated = False

    def _calibrate_exposure(self, bayer8: np.ndarray) -> np.ndarray:
        """Nudge hardware analogue_gain toward a target brightness, once per session.

        cfg.analogue_gain was tuned for a dark room (see docs/DEBUGGING.md);
        in brighter conditions it overexposes badly enough that channels
        clip before white-balance correction can help — observed raw means
        as high as 235/255 with the fixed gain, with the room's actual
        brightness swinging more than 2x between test runs minutes apart.
        This is a one-shot startup calibration from the first real frame
        (an initial brightness-average check, then up to 3 bounded
        follow-up passes), not a continuous AE loop (see #3 for that). The
        follow-up passes specifically check the clipped-pixel fraction
        rather than the whole-frame average, since a bright subregion
        (e.g. a face) can stay badly clipped even when the overall average
        already looks fine if the rest of the scene is dim enough to
        balance it out — a single fixed reduction step wasn't always
        enough either, so each pass steps harder (0.5x vs 0.7x) when
        clipping is more severe and stops once it's resolved or the gain
        floor is reached. If the frame is already in a reasonable range
        with no significant clipping, no change is made.

        Args:
            bayer8: 2-D uint8 Bayer mosaic from the first real frame.

        Returns:
            A frame captured after the last gain change settles, or the
            original frame unchanged if no adjustment was needed.
        """
        b = bayer8[: self._h2, : self._w2]
        brightness = float(b.mean())
        target = 128.0
        gain = self.cfg.analogue_gain

        if 90.0 <= brightness <= 170.0:
            log.info('Exposure OK at startup (brightness=%.1f/255), no gain adjustment', brightness)
        else:
            ratio = target / max(brightness, 1.0)
            gain = max(16, min(248, round(gain * ratio)))
            log.info(
                'Adjusting analogue_gain %d -> %d (brightness=%.1f/255, target=%.0f)',
                self.cfg.analogue_gain,
                gain,
                brightness,
                target,
            )
            settled = self._apply_gain_and_settle(gain)
            if settled is not None:
                bayer8 = settled

        # A bright subregion (e.g. a face) can stay significantly clipped
        # even when the whole-frame average already looks fine, if the
        # rest of the scene is dim enough to balance it out — reported
        # live as a face that looks blown out ("like headlights") despite
        # the calibration log showing reasonable numbers (those numbers
        # exclude the clipped region entirely, see
        # _calibrate_white_balance). Check the clipped-pixel fraction
        # specifically and reduce gain further if it's still high.
        #
        # Iterates (bounded) rather than a single fixed step: a scene with
        # a lot of dynamic range between subject and background needed
        # more than one 0.7x reduction to actually clear (observed 75%
        # still clipped after the first follow-up pass alone). Steps
        # harder when clipping is more severe. See #16.
        for _ in range(3):
            clipped_fraction = float((bayer8[: self._h2, : self._w2] >= 250).mean())
            if clipped_fraction <= 0.10:
                break
            step = 0.5 if clipped_fraction > 0.5 else 0.7
            new_gain = max(16, round(gain * step))
            if new_gain == gain:
                break  # already at the gain floor, further attempts won't help
            gain = new_gain
            log.info(
                '%.0f%% of frame still clipped — reducing analogue_gain further to %d', clipped_fraction * 100, gain
            )
            settled = self._apply_gain_and_settle(gain)
            if settled is None:
                break
            bayer8 = settled

        return bayer8

    def _apply_gain_and_settle(self, new_gain: int) -> np.ndarray | None:
        """Write a new analogue_gain value and capture a frame after it settles.

        Args:
            new_gain: Value to set for the sensor's analogue_gain control.

        Returns:
            Unpacked bayer8 from a frame captured after the change, or
            None if no frame arrived within the timeout.
        """
        sensor_entity = find_sensor_entity(self.cfg.media_device)
        subdev = run_cmd(['media-ctl', '-d', self.cfg.media_device, '-e', sensor_entity]).stdout.strip()
        run_cmd(['v4l2-ctl', '-d', subdev, '-c', f'analogue_gain={new_gain}'])

        raw = None
        for _ in range(4):
            r, _, _ = select.select([self.fd], [], [], 2.0)
            if not r:
                break
            buf = V4L2Buffer()
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
            buf.memory = V4L2_MEMORY_MMAP
            fcntl.ioctl(self.fd, VIDIOC_DQBUF, buf)
            self.buffers[buf.index].seek(0)
            raw = np.frombuffer(self.buffers[buf.index].read(buf.bytesused), dtype=np.uint8)
            fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)
        return self._unpack_sgrbg10(raw) if raw is not None else None

    def _calibrate_white_balance(self, bayer8: np.ndarray) -> None:
        """Derive gray-world white-balance gains from one real frame.

        Computes the mean raw value at each Bayer quad position and scales
        R/B gains to match G's mean, so a roughly neutral scene renders
        without a color cast. Clamped to a modest range to avoid extreme
        correction on a genuinely non-neutral scene. Runs once per stream
        start (see read_frame()), not every frame — cheap, and avoids
        gain hunting from frame-to-frame scene variation.

        2x2 quads with any near-clipped channel (>=250/255) are excluded
        from the average. A face filling most of the frame isn't neutral
        gray (real red bias) and, if bright/near-blown-out, skews the
        whole-frame gray-world ratio hard enough to produce a visible cast
        on the actually-neutral background — observed as a bright/white
        face with a green-tinted background. See #16 and docs/DEBUGGING.md.

        Args:
            bayer8: 2-D uint8 Bayer mosaic to calibrate from.
        """
        b = bayer8[: self._h2, : self._w2]
        r_ch = b[0::2, 1::2]
        g1_ch = b[0::2, 0::2]
        g2_ch = b[1::2, 1::2]
        b_ch = b[1::2, 0::2]

        valid = (r_ch < 250) & (g1_ch < 250) & (g2_ch < 250) & (b_ch < 250)
        if valid.sum() < valid.size * 0.05:
            # Almost everything is clipped (e.g. a very bright scene) —
            # excluding it all would leave too few samples to be
            # meaningful, so fall back to the full, unmasked frame.
            valid = np.ones_like(valid)

        r_mean = r_ch[valid].astype(np.float64).mean()
        g_mean = (g1_ch[valid].astype(np.float64).mean() + g2_ch[valid].astype(np.float64).mean()) / 2
        b_mean = b_ch[valid].astype(np.float64).mean()

        def gain(target: float, source: float) -> np.uint16:
            if source < 1:
                return np.uint16(64)
            ratio = min(max(target / source, 0.5), 3.0)
            return np.uint16(round(ratio * 64))

        self._sr = gain(g_mean, r_mean)
        self._sb = gain(g_mean, b_mean)
        self._wb_calibrated = True
        log.info(
            'White balance calibrated from R=%.1f G=%.1f B=%.1f: sr=%d sg=64 sb=%d (/64)',
            r_mean,
            g_mean,
            b_mean,
            self._sr,
            self._sb,
        )

    def open(self) -> None:
        """Open the capture device, configure the pipeline, and start streaming.

        Media pipeline (links + subdev pad formats) is configured once here,
        before REQBUFS/QBUF — reconfiguring it afterwards, with buffers
        already queued for the old format, caused VIDIOC_STREAMON to fail
        with BrokenPipeError on any run after the first (dmesg showed CSI2
        "Frame sync error" / "Transfer FIFO overflow"). See docs/DEBUGGING.md.
        """
        self.fd = os.open(self.cfg.capture_device, os.O_RDWR | os.O_NONBLOCK)
        log.info('Opened %s (fd=%d)', self.cfg.capture_device, self.fd)

        set_sensor_gain(self.cfg)
        setup_media_pipeline(self.cfg)

        fmt = V4L2Format()
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        fmt.fmt.pix.width = self.cfg.sensor_width
        fmt.fmt.pix.height = self.cfg.sensor_height
        fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_SGRBG10
        fmt.fmt.pix.field = 1
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)
        self.frame_size = fmt.fmt.pix.sizeimage
        self.stride = fmt.fmt.pix.bytesperline
        log.info(
            'Format: %dx%d SGRBG10 (unpacked), stride=%d, frame_size=%d bytes',
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

        buf_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE)
        fcntl.ioctl(self.fd, VIDIOC_STREAMON, buf_type)
        log.info('Streaming started')

    def _unpack_sgrbg10(self, raw: np.ndarray) -> np.ndarray:
        """Unpack an unpacked-10-bit (V4L2_PIX_FMT_SGRBG10) frame to 8-bit.

        Each pixel is a 16-bit little-endian value with the 10 significant
        bits in the low bits (upper 6 bits zero-padded). Right-shifting by 2
        recovers the same 8-bit approximation the previous packed-format
        unpacking used (top 8 of the 10 significant bits). The row stride is
        padded and must be respected when reshaping.

        Args:
            raw: Flat uint8 array of the raw frame buffer as read from mmap.

        Returns:
            2-D uint8 array of shape (sensor_height, sensor_width) containing
            the 8-bit Bayer mosaic values.
        """
        h, w, stride = self.cfg.sensor_height, self.cfg.sensor_width, self.stride
        raw16 = raw.view(np.uint16)
        stride_pixels = stride // 2
        rows = raw16[: h * stride_pixels].reshape(h, stride_pixels)
        return (rows[:, :w] >> 2).astype(np.uint8)

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

        bayer8 = self._unpack_sgrbg10(raw)
        if not self._wb_calibrated:
            bayer8 = self._calibrate_exposure(bayer8)
            self._calibrate_white_balance(bayer8)
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
                as produced by _unpack_sgrbg10.

        Returns:
            Memoryview into a pre-allocated bytearray (output_height *
            output_width * 4 bytes, BGRx format).
        """
        b = bayer8[: self._h2, : self._w2]

        # 2x2 block average debayer into pre-allocated half-res channel
        # arrays. GRBG layout: G on the main diagonal (0,0)+(1,1), R at
        # (0,1), B at (1,0) — matches the SGRBG10_1X10 media bus format
        # actually negotiated with the hardware. A prior attempt to "fix" a
        # magenta cast by swapping to an RGGB assumption was based on a
        # misdiagnosis (the real bug at the time was elsewhere, in since-
        # replaced packed-format unpacking) — confirmed wrong by directly
        # comparing per-quad-position channel means from a real capture:
        # the RGGB assignment produced R≈B with G suppressed (the magenta
        # signature), while this GRBG assignment gives well-separated
        # R/G/B means. See docs/DEBUGGING.md.
        # np.add(uint8, uint8, out=uint16) computes the addition in uint8
        # BEFORE widening to write into `out` — casting='unsafe' only
        # permits the final cast, it doesn't make the arithmetic happen in
        # the wider type. Two bright G samples (e.g. 200+200) silently
        # wrapped mod 256 instead of summing to 400, corrupting the green
        # channel in bright regions. Fix: copy (safe widening cast, not an
        # arithmetic op) into the uint16 accumulator first, then += so the
        # addition itself happens in uint16. See docs/DEBUGGING.md.
        np.copyto(self._G, b[0::2, 0::2], casting='unsafe')
        self._G += b[1::2, 1::2]
        self._G //= 2
        np.copyto(self._R, b[0::2, 1::2], casting='unsafe')
        np.copyto(self._B, b[1::2, 0::2], casting='unsafe')

        # Gather straight from half-res Bayer channels to output resolution (one
        # fancy-index copy per channel, no full-res intermediate) then apply
        # gray-world white balance (self._sr/_sg/_sb, calibrated once per
        # session in _calibrate_white_balance() — see docs/DEBUGGING.md and
        # #3 for real per-frame auto-gain as a further step beyond this).
        np.multiply(self._R[np.ix_(self._y_idx_half, self._x_idx_half)], self._sr, out=self._out_r, casting='unsafe')
        np.multiply(self._G[np.ix_(self._y_idx_half, self._x_idx_half)], self._sg, out=self._out_g, casting='unsafe')
        np.multiply(self._B[np.ix_(self._y_idx_half, self._x_idx_half)], self._sb, out=self._out_b, casting='unsafe')
        self._out_r >>= 6
        self._out_g >>= 6
        self._out_b >>= 6
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
