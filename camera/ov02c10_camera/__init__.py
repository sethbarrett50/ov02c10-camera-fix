"""OV02C10 IPU6 camera preview for Dell XPS 16 on Debian 13.

Captures raw Bayer frames from the OV02C10 sensor via V4L2 mmap, debayers
them in software, and feeds the result into a GStreamer pipeline for
display (gtksink) and/or a v4l2loopback virtual camera for use in
browsers.
"""

__version__ = '0.1.0'
