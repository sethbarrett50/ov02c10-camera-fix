"""Tests for CameraConfig defaults."""

from ov02c10_camera.config import CameraConfig


def test_defaults_match_known_working_hardware_values() -> None:
    """Defaults should stay pinned to values confirmed working on real hardware.

    These aren't arbitrary — see docs/DEBUGGING.md. sensor_width/height match
    the OV02C10's native output, and analogue_gain/digital_gain are the pair
    that fixed the "exposure pinned at max, gain near floor" dark-frame issue.
    """
    cfg = CameraConfig()
    assert cfg.sensor_width == 1928
    assert cfg.sensor_height == 1092
    assert cfg.sensor_format == 'SGRBG10_1X10'
    assert cfg.analogue_gain == 150
    assert cfg.digital_gain == 4096
    assert cfg.use_loopback is False


def test_config_is_overridable() -> None:
    """CLI flags override individual fields without touching the rest."""
    cfg = CameraConfig(analogue_gain=200, output_width=640, output_height=480)
    assert cfg.analogue_gain == 200
    assert cfg.output_width == 640
    assert cfg.output_height == 480
    # Untouched fields keep their defaults
    assert cfg.digital_gain == 4096
