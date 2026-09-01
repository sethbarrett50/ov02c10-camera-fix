---
name: Bug report
about: Something isn't working right
title: ""
labels: bug
---

**Describe the bug**
A clear description of what's wrong.

**Hardware / environment**
- Laptop model:
- Sensor (`media-ctl -d /dev/media0 -p | grep -i ov02`):
- Kernel version (`uname -r`):
- Distro/version:

**Diagnostic output**
Paste relevant output from:
```bash
v4l2-ctl -d "$(media-ctl -d /dev/media0 -e 'ov02c10 <BUS>-0036')" -l
journalctl --user -u ov02c10-camera -n 100
```

**Expected behavior**
What you expected to happen instead.
