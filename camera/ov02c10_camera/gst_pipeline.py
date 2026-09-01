"""GStreamer pipeline: feeds V4L2Camera frames into a viewfinder or v4l2loopback sink."""

import logging
import signal
import threading

from .camera import V4L2Camera
from .config import CameraConfig

log = logging.getLogger(__name__)


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
