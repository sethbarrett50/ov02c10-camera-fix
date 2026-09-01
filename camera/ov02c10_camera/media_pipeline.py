"""media-ctl/v4l2-ctl wiring: sensor discovery, gain control, link/format setup."""

import logging
import re
import subprocess
import time

from .config import CameraConfig

log = logging.getLogger(__name__)


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
        # Sets the sensor's own output pad format. Without this, the sensor
        # keeps whatever format the last process (e.g. `cam`) configured it
        # to, which can silently mismatch what we tell the CSI2 receiver to
        # expect below — the likely cause of the CSI2 "Frame sync error" /
        # "Transfer FIFO overflow" errors on VIDIOC_STREAMON after the first
        # run. `cam`/libcamera sets this via VIDIOC_SUBDEV_S_FMT on every
        # invocation (confirmed via strace); this had no equivalent here.
        # See docs/DEBUGGING.md.
        ('sensor fmt', ['media-ctl', '-d', cfg.media_device, '--set-v4l2', f'"{sensor_entity}":0[fmt:{fmt}]']),
        ('IVSC sink fmt', ['media-ctl', '-d', cfg.media_device, '--set-v4l2', f'"Intel IVSC CSI":0[fmt:{fmt}]']),
        ('IVSC source fmt', ['media-ctl', '-d', cfg.media_device, '--set-v4l2', f'"Intel IVSC CSI":1[fmt:{fmt}]']),
        ('CSI2-4 sink fmt', ['media-ctl', '-d', cfg.media_device, '--set-v4l2', f'"Intel IPU6 CSI2 4":0[fmt:{fmt}]']),
        ('CSI2-4 source fmt', ['media-ctl', '-d', cfg.media_device, '--set-v4l2', f'"Intel IPU6 CSI2 4":1[fmt:{fmt}]']),
    ]
    for desc, cmd in steps:
        r = run_cmd(cmd, check=False)
        log.info('  %s %s', '✓' if r.returncode == 0 else '⚠', desc)


CRITICAL_PROCESSES = {'pipewire', 'wireplumber', 'pipewire-media-session'}


def free_device(device: str) -> None:
    """Kill any non-critical process holding the given device node open.

    Never kills system multimedia services (PipeWire/WirePlumber) even if
    fuser reports them holding the device — WirePlumber holds a brief
    monitoring/enumeration handle on camera devices as part of normal
    desktop media-session management, not an exclusive streaming lock. An
    earlier version of this function used `fuser -k <device>` unconditionally,
    which killed PipeWire itself every time this app started, taking the
    whole system's audio routing down along with it (discovered because a
    live Teams call's microphone stopped working every time the camera
    service ran). See docs/DEBUGGING.md.

    Args:
        device: Path to the device node to check, e.g. '/dev/video32'.
    """
    result = run_cmd(['fuser', device], check=False)
    pids = result.stdout.split()
    if not pids:
        log.info('Device %s is free', device)
        return

    to_kill = []
    for pid in pids:
        name = run_cmd(['ps', '-p', pid, '-o', 'comm='], check=False).stdout.strip()
        if name in CRITICAL_PROCESSES:
            log.warning('  %s (pid %s) holds %s — leaving it alone (normal media-session handle)', name, pid, device)
        else:
            to_kill.append(pid)

    if to_kill:
        log.warning('Device %s held by PIDs %s — killing', device, ' '.join(to_kill))
        run_cmd(['kill', '-9', *to_kill], check=False)
        time.sleep(1)
    else:
        log.info('Device %s: only critical services holding it, proceeding', device)
