"""Configuration for the OV02C10 camera and output pipeline."""

from dataclasses import dataclass


@dataclass
class CameraConfig:
    """Configuration for the OV02C10 camera and output pipeline.

    Attributes:
        capture_device: V4L2 device node for the IPU6 capture endpoint.
        media_device: Media controller device for pipeline configuration.
        sensor_width: Native sensor width in pixels including padding.
        sensor_height: Native sensor height in pixels.
        sensor_format: Media bus format string for IPU6 link configuration.
            This is a hardware-negotiation label for the CSI2 receiver —
            'SGRBG10_1X10' is what this hardware/driver actually validates
            (matches what libcamera/cam always uses) and, per a direct
            per-quad-position channel-mean comparison on a real capture,
            also matches the sensor's true physical Bayer order (GRBG
            indexing in camera.py._debayer()). Changing this string to
            'SRGGB10_1X10' broke VIDIOC_STREAMON entirely — it's a hardware
            negotiation value, not a free software-side relabeling.
            See docs/DEBUGGING.md.
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
