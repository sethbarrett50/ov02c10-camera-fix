"""Tests for media-ctl/v4l2-ctl wiring — mocked subprocess calls, no hardware needed."""

import subprocess

import pytest

from ov02c10_camera import media_pipeline


def _fake_completed_process(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    """Build a CompletedProcess for mocking subprocess.run.

    Args:
        stdout: Text to return as stdout.
        returncode: Exit code to simulate.

    Returns:
        A CompletedProcess matching what subprocess.run(..., text=True) yields.
    """
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr='')


def test_find_sensor_entity_extracts_current_bus_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """The I2C bus number varies per boot — regex must pull whatever's live.

    This is the exact bug found in production: main.py hardcoded 'ov02c10
    14-0036', but the real bus was '5-0036' on a later boot. See
    docs/DEBUGGING.md.
    """
    media_ctl_output = (
        '- entity 368: ov02c10 5-0036 (1 pad, 1 link, 0 routes)\n'
        '            type V4L2 subdev subtype Sensor flags 0\n'
        '            device node name /dev/v4l-subdev3\n'
    )
    monkeypatch.setattr(
        media_pipeline.subprocess,
        'run',
        lambda *a, **k: _fake_completed_process(media_ctl_output),
    )
    assert media_pipeline.find_sensor_entity('/dev/media0') == 'ov02c10 5-0036'


def test_find_sensor_entity_raises_when_sensor_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A topology with no ov02c10 entity should fail loudly, not silently."""
    monkeypatch.setattr(
        media_pipeline.subprocess,
        'run',
        lambda *a, **k: _fake_completed_process('- entity 1: some other camera (1 pad, 1 link, 0 routes)\n'),
    )
    with pytest.raises(RuntimeError, match='ov02c10 sensor entity not found'):
        media_pipeline.find_sensor_entity('/dev/media0')


def test_run_cmd_captures_stdout_on_success() -> None:
    """run_cmd should behave like a thin, non-hardware subprocess.run wrapper."""
    result = media_pipeline.run_cmd(['echo', 'hello'])
    assert result.returncode == 0
    assert result.stdout.strip() == 'hello'


def test_run_cmd_does_not_raise_on_nonzero_exit() -> None:
    """check=True only logs a warning, it never raises — callers inspect returncode."""
    result = media_pipeline.run_cmd(['false'])
    assert result.returncode != 0


def test_free_device_never_kills_pipewire_or_wireplumber(monkeypatch: pytest.MonkeyPatch) -> None:
    """PipeWire/WirePlumber holding the camera device is normal, not a conflict.

    An earlier version killed whatever fuser reported unconditionally,
    which killed the system's whole PipeWire multimedia service (and with
    it, live audio/mic routing) every time this app started. See
    docs/DEBUGGING.md.
    """
    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if cmd[0] == 'fuser':
            return _fake_completed_process('477393 477395\n')
        if cmd[0] == 'ps':
            names = {'477393': 'pipewire', '477395': 'wireplumber'}
            return _fake_completed_process(names.get(cmd[2], '') + '\n')
        return _fake_completed_process('')

    monkeypatch.setattr(media_pipeline, 'run_cmd', fake_run_cmd)
    monkeypatch.setattr(media_pipeline.time, 'sleep', lambda *_: None)

    media_pipeline.free_device('/dev/video32')

    kill_calls = [c for c in calls if c[0] == 'kill']
    assert kill_calls == []


def test_free_device_kills_genuinely_conflicting_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-critical process actually holding the device still gets killed."""
    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if cmd[0] == 'fuser':
            return _fake_completed_process('12345\n')
        if cmd[0] == 'ps':
            return _fake_completed_process('some_other_app\n')
        return _fake_completed_process('')

    monkeypatch.setattr(media_pipeline, 'run_cmd', fake_run_cmd)
    monkeypatch.setattr(media_pipeline.time, 'sleep', lambda *_: None)

    media_pipeline.free_device('/dev/video32')

    kill_calls = [c for c in calls if c[0] == 'kill']
    assert len(kill_calls) == 1
    assert '12345' in kill_calls[0]
