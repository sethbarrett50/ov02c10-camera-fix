"""Command-line entry point: argument parsing and pipeline startup."""

import argparse
import logging
import sys

from .config import CameraConfig
from .gst_pipeline import run_pipeline
from .logging_setup import configure_logging
from .media_pipeline import free_device

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser for the ov02c10-camera entry point.
    """
    parser = argparse.ArgumentParser(description='OV02C10 IPU6 camera preview — Dell XPS 16 / Debian 13')
    parser.add_argument('--device', default='/dev/video32', help='V4L2 capture device (default: /dev/video32)')
    parser.add_argument('--width', type=int, default=1280, help='Output width in pixels (default: 1280)')
    parser.add_argument('--height', type=int, default=720, help='Output height in pixels (default: 720)')
    parser.add_argument(
        '--loopback', action='store_true', help='Feed /dev/video48 v4l2loopback instead of display window'
    )
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
    return parser


def main() -> int:
    """Parse arguments and launch the camera pipeline.

    Returns:
        Exit code from run_pipeline.
    """
    args = build_parser().parse_args()
    configure_logging(verbose=args.verbose)

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

    free_device(cfg.capture_device)
    return run_pipeline(cfg)


if __name__ == '__main__':
    sys.exit(main())
