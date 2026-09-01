"""
OV02C10 IPU6 camera preview for Dell XPS 16 on Debian 13.

Captures raw Bayer frames from the OV02C10 sensor via V4L2 mmap, debayers
them in software, and feeds the result into a GStreamer pipeline for display
(gtksink) and/or a v4l2loopback virtual camera for use in browsers.

Usage:
    uv run main.py [--device /dev/video32] [--width 1280] [--height 720]
                   [--loopback] [--no-setup] [--verbose]
"""

import argparse
import ctypes
import fcntl
import logging
import mmap
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time

from dataclasses import dataclass as dc

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('camera')

VIDIOC_S_FMT = 0xC0D05605
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_PIX_FMT_pgAA = 0x41416770


class V4L2PixFormat(ctypes.Structure):
    _fields_ = [
        ('width', ctypes.c_uint32),
        ('height', ctypes.c_uint32),
        ('pixelformat', ctypes.c_uint32),
        ('field', ctypes.c_uint32),
        ('bytesperline', ctypes.c_uint32),
        ('sizeimage', ctypes.c_uint32),
        ('colorspace', ctypes.c_uint32),
        ('priv', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('ycbcr_enc', ctypes.c_uint32),
        ('quantization', ctypes.c_uint32),
        ('xfer_func', ctypes.c_uint32),
    ]


class V4L2FmtUnion(ctypes.Union):
    _fields_ = [('pix', V4L2PixFormat), ('raw', ctypes.c_uint8 * 200)]


class V4L2Format(ctypes.Structure):
    _fields_ = [('type', ctypes.c_uint32), ('_pad', ctypes.c_uint32), ('fmt', V4L2FmtUnion)]


class V4L2RequestBuffers(ctypes.Structure):
    _fields_ = [
        ('count', ctypes.c_uint32),
        ('type', ctypes.c_uint32),
        ('memory', ctypes.c_uint32),
        ('capabilities', ctypes.c_uint32),
        ('flags', ctypes.c_uint8),
        ('reserved', ctypes.c_uint8 * 3),
    ]


class V4L2Timecode(ctypes.Structure):
    _fields_ = [
        ('type', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('frames', ctypes.c_uint8),
        ('seconds', ctypes.c_uint8),
        ('minutes', ctypes.c_uint8),
        ('hours', ctypes.c_uint8),
        ('userbits', ctypes.c_uint8 * 4),
    ]


class V4L2BufferM(ctypes.Union):
    _fields_ = [
        ('offset', ctypes.c_uint32),
        ('userptr', ctypes.c_ulong),
        ('planes', ctypes.c_void_p),
        ('fd', ctypes.c_int32),
    ]


class V4L2Buffer(ctypes.Structure):
    _fields_ = [
        ('index', ctypes.c_uint32),
        ('type', ctypes.c_uint32),
        ('bytesused', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('field', ctypes.c_uint32),
        ('tv_sec', ctypes.c_long),
        ('tv_usec', ctypes.c_long),
        ('timecode', V4L2Timecode),
        ('sequence', ctypes.c_uint32),
        ('memory', ctypes.c_uint32),
        ('m', V4L2BufferM),
        ('length', ctypes.c_uint32),
        ('reserved2', ctypes.c_uint32),
        ('request_fd', ctypes.c_int32),
    ]


@dc
class CameraConfig:
    """Configuration for the OV02C10 camera and output pipeline.

    Attributes:
        capture_device: V4L2 device node for the IPU6 capture endpoint.
        media_device: Media controller device for pipeline configuration.
        sensor_width: Native sensor width in pixels including padding.
        sensor_height: Native sensor height in pixels.
        sensor_format: Media bus format string for IPU6 link configuration.
        output_width: Display/loopback output width after downscaling.
        output_height: Display/loopback output height after downscaling.
        framerate: Target output framerate.
        loopback_device: v4l2loopback device node for browser/app access.
        use_loopback: If True, feed output to loopback device instead of display.
        num_buffers: Number of V4L2 kernel mmap buffers to allocate.
        analogue_gain: Sensor analogue_gain control value (range 16-248).
            There is no active AE/AGC loop for this raw capture path, so the
            sensor otherwise sits at its power-on defaults (exposure pinned
            at max, gain near the floor). This value was tuned for a
            well-lit room — revisit if the capture environment changes.
        digital_gain: Sensor digital_gain control value (range 1024-16383).
    """

    capture_device: str = '/dev/video32'
    media_device: str = '/dev/media0'
    sensor_width: int = 1928
    sensor_height: int = 1092
    sensor_format: str = 'SGRBG10_1X10'
    output_width: int = 1280
    output_height: int = 720
    framerate: int = 30
    loopback_device: str = '/dev/video48'
    use_loopback: bool = False
    num_buffers: int = 4
    analogue_gain: int = 150
    digital_gain: int = 4096


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess command and optionally warn on failure.

    Args:
        cmd: Command and arguments as a list of strings.
        check: If True, log a warning when the command returns non-zero.

    Returns:
        The completed process result.
    """
    log.debug('run: %s', ' '.join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        log.warning('Command failed: %s\n%s', ' '.join(cmd), result.stderr.strip())
    return result


def find_sensor_entity(media_device: str) -> str:
    """Resolve the ov02c10 sensor's current media entity name.

    The I2C bus number embedded in the entity name (e.g. 'ov02c10 5-0036')
    is not stable across boots on this hardware — ACPI enumerates I2C
    devices in a different order depending on boot state. A previous
    version of this file hardcoded 'ov02c10 14-0036', which had gone stale
    and made every media-ctl link/format call in setup_media_pipeline() fail
    silently (they run with check=False). Querying the live topology avoids
    hardcoding a bus number that can silently go stale again.

    Args:
        media_device: Media controller device node, e.g. '/dev/media0'.

    Returns:
        The current sensor entity name, e.g. 'ov02c10 5-0036'.

    Raises:
        RuntimeError: If no ov02c10 entity is found in the media topology.
    """
    result = subprocess.run(['media-ctl', '-d', media_device, '-p'], capture_output=True, text=True, check=True)
    match = re.search(r'ov02c10 \d+-[0-9a-f]{4}', result.stdout)
    if not match:
        raise RuntimeError(f'ov02c10 sensor entity not found in {media_device} topology')
    return match.group(0)


def set_sensor_gain(cfg: CameraConfig) -> None:
    """Set analogue and digital gain on the sensor subdevice.

    There is no active AE/AGC loop anywhere in this raw capture path, so
    without this the sensor sits at its power-on defaults: exposure pinned
    at its maximum and gain left near the floor, producing a very dark raw
    signal that the debayer step can't fully recover.

    Args:
        cfg: Camera configuration specifying the desired gain values.
    """
    sensor_entity = find_sensor_entity(cfg.media_device)
    subdev = run_cmd(['media-ctl', '-d', cfg.media_device, '-e', sensor_entity]).stdout.strip()
    ctrl = f'analogue_gain={cfg.analogue_gain},digital_gain={cfg.digital_gain}'
    r = run_cmd(['v4l2-ctl', '-d', subdev, '-c', ctrl])
    log.info('  %s Set sensor gain (%s) on %s', '✓' if r.returncode == 0 else '⚠', ctrl, subdev)


def setup_media_pipeline(cfg: CameraConfig) -> None:
    """Configure the IPU6 media pipeline using media-ctl.

    Sets up the link from the OV02C10 sensor through the Intel IVSC CSI
    bridge and IPU6 CSI2 receiver to the ISYS capture node, and configures
    the SGRBG10 format on all pads.

    Args:
        cfg: Camera configuration specifying sensor dimensions and format.
    """
    log.info('Configuring media pipeline on %s', cfg.media_device)
    sensor_entity = find_sensor_entity(cfg.media_device)
    fmt = f'{cfg.sensor_format}/{cfg.sensor_width}x{cfg.sensor_height}'
    steps = [
        (
            'sensor->IVSC link',
            ['media-ctl', '-d', cfg.media_device, '--links', f'"{sensor_entity}":0->"Intel IVSC CSI":0[1]'],
        ),
        (
            'CSI2-4->Capture32 link',
            [
                'media-ctl',
                '-d',
                cfg.media_device,
                '--links',
                '"Intel IPU6 CSI2 4":1->"Intel IPU6 ISYS Capture 32":0[1]',
            ],
        ),
        ('IVSC sink fmt', ['media-ctl', '-d', cfg.media_device, '--set-v4l2', f'"Intel IVSC CSI":0[fmt:{fmt}]']),
        ('IVSC source fmt', ['media-ctl', '-d', cfg.media_device, '--set-v4l2', f'"Intel IVSC CSI":1[fmt:{fmt}]']),
        ('CSI2-4 sink fmt', ['media-ctl', '-d', cfg.media_device, '--set-v4l2', f'"Intel IPU6 CSI2 4":0[fmt:{fmt}]']),
        ('CSI2-4 source fmt', ['media-ctl', '-d', cfg.media_device, '--set-v4l2', f'"Intel IPU6 CSI2 4":1[fmt:{fmt}]']),
    ]
    for desc, cmd in steps:
        r = run_cmd(cmd, check=False)
        log.info('  %s %s', '✓' if r.returncode == 0 else '⚠', desc)


def free_device(device: str) -> None:
    """Kill any process holding the given device node open.

    Args:
        device: Path to the device node to check, e.g. '/dev/video32'.
    """
    result = run_cmd(['fuser', device], check=False)
    if result.stdout.strip():
        log.warning('Device held by PIDs %s — killing', result.stdout.strip())
        run_cmd(['fuser', '-k', device], check=False)
        time.sleep(1)
    else:
        log.info('Device %s is free', device)


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

        log.debug(
            'Frame %d: %d bytes -> %dx%d BGRx',
            buf.sequence,
            buf.bytesused,
            self.cfg.output_width,
            self.cfg.output_height,
        )
        return memoryview(self._out_buf)

    def close(self) -> None:
        """Stop streaming, unmap buffers, and close the device fd."""
        if self.fd >= 0:
            buf_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE)
            try:
                fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, buf_type)
            except Exception:
                pass
            for mm in self.buffers:
                mm.close()
            os.close(self.fd)
            self.fd = -1
            log.info('Camera closed')


def run_pipeline(cfg: CameraConfig) -> int:
    """Build and run the GStreamer pipeline.

    In display mode (use_loopback=False), opens a gtksink preview window.
    In loopback mode (use_loopback=True), feeds frames to the v4l2loopback
    virtual camera device without a display window, suitable for running as
    a background systemd service.

    Args:
        cfg: Camera configuration.

    Returns:
        Exit code (0 on clean exit, 1 on import error).
    """
    try:
        import gi

        gi.require_version('Gst', '1.0')
        gi.require_version('GLib', '2.0')
        from gi.repository import GLib, Gst  # type: ignore
    except ImportError as e:
        log.error('GStreamer Python bindings not available: %s', e)
        return 1

    Gst.init(None)
    log.info('GStreamer %s', Gst.version_string())

    if cfg.use_loopback:
        sink_str = (
            f'queue max-size-buffers=2 leaky=downstream '
            f'! videoconvert '
            f'! video/x-raw,format=YUY2,'
            f'width={cfg.output_width},height={cfg.output_height} '
            f'! v4l2sink device={cfg.loopback_device} sync=false'
        )
    else:
        sink_str = 'gtksink sync=false'

    pipeline = Gst.parse_launch(f'appsrc name=src ! {sink_str}')
    appsrc = pipeline.get_by_name('src')

    caps = Gst.Caps.from_string(
        f'video/x-raw,format=BGRx,width={cfg.output_width},height={cfg.output_height},framerate={cfg.framerate}/1'
    )
    appsrc.set_property('caps', caps)
    appsrc.set_property('is-live', True)
    appsrc.set_property('format', Gst.Format.TIME)
    appsrc.set_property('stream-type', 0)
    appsrc.set_property('block', False)
    log.info('appsrc caps: %s', caps.to_string())

    cam = V4L2Camera(cfg)
    cam.open()

    loop = GLib.MainLoop()
    running = True

    def feed_thread() -> None:
        """Read frames from V4L2 and push them into the GStreamer appsrc."""
        nonlocal running
        pts = 0
        duration = Gst.SECOND // cfg.framerate
        log.info('Feed thread started')
        while running:
            view = cam.read_frame()
            if view is None:
                continue
            # new_allocate+fill instead of new_wrapped(bytes(view)): new_wrapped
            # ties a fresh Python bytes object to each GstBuffer's lifetime every
            # frame, which was never being reclaimed and drove RSS into the GBs
            # within seconds at 30fps.
            gst_buf = Gst.Buffer.new_allocate(None, len(view), None)
            gst_buf.fill(0, bytes(view))
            gst_buf.pts = pts
            gst_buf.duration = duration
            pts += duration
            ret = appsrc.emit('push-buffer', gst_buf)
            del gst_buf
            if ret != Gst.FlowReturn.OK:
                log.error('push-buffer failed: %s', ret)
                running = False
                loop.quit()
                break
        appsrc.emit('end-of-stream')
        log.info('Feed thread done')

    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(bus, msg) -> None:
        """Handle GStreamer bus messages for EOS and warnings."""
        nonlocal running
        if msg.type == Gst.MessageType.EOS:
            log.info('EOS')
            running = False
            loop.quit()
        elif msg.type == Gst.MessageType.WARNING:
            warn, _ = msg.parse_warning()
            log.warning('GST warning: %s', warn.message)

    bus.connect('message', on_message)

    def on_sigint(signum, frame) -> None:
        """Handle SIGINT for clean shutdown."""
        nonlocal running
        log.info('Interrupted')
        running = False
        loop.quit()

    signal.signal(signal.SIGINT, on_sigint)

    t = threading.Thread(target=feed_thread, daemon=True)
    t.start()
    pipeline.set_state(Gst.State.PLAYING)
    log.info('Running — Ctrl+C to stop')
    loop.run()

    running = False
    t.join(timeout=2)
    pipeline.set_state(Gst.State.NULL)
    cam.close()
    return 0


def main() -> int:
    """Parse arguments and launch the camera pipeline.

    Returns:
        Exit code from run_pipeline.
    """
    parser = argparse.ArgumentParser(description='OV02C10 IPU6 camera preview — Dell XPS 16 / Debian 13')
    parser.add_argument('--device', default='/dev/video32', help='V4L2 capture device (default: /dev/video32)')
    parser.add_argument('--width', type=int, default=1280, help='Output width in pixels (default: 1280)')
    parser.add_argument('--height', type=int, default=720, help='Output height in pixels (default: 720)')
    parser.add_argument(
        '--loopback', action='store_true', help='Feed /dev/video48 v4l2loopback instead of display window'
    )
    parser.add_argument('--no-setup', action='store_true', help='Skip media pipeline configuration')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable debug-level logging')
    parser.add_argument(
        '--analogue-gain',
        type=int,
        default=150,
        help='Sensor analogue_gain control value, range 16-248 (default: 150)',
    )
    parser.add_argument(
        '--digital-gain',
        type=int,
        default=4096,
        help='Sensor digital_gain control value, range 1024-16383 (default: 4096)',
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = CameraConfig(
        capture_device=args.device,
        output_width=args.width,
        output_height=args.height,
        use_loopback=args.loopback,
        analogue_gain=args.analogue_gain,
        digital_gain=args.digital_gain,
    )

    log.info('=== OV02C10 IPU6 Camera — Dell XPS 16 / Debian 13 ===')
    log.info(
        'Sensor: %dx%d  ->  output: %dx%d', cfg.sensor_width, cfg.sensor_height, cfg.output_width, cfg.output_height
    )

    if not args.no_setup:
        setup_media_pipeline(cfg)

    free_device(cfg.capture_device)
    return run_pipeline(cfg)


if __name__ == '__main__':
    sys.exit(main())
