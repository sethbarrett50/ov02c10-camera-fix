# Contributing

- `main` is the stable/default branch — only updated by fast-forwarding from `dev`.
- `dev` is the integration branch for ongoing work.
- Branch features/fixes off `dev`: `git checkout dev && git checkout -b feature/whatever`.
- Open PRs **against `dev`**, not `main`.

## Before opening a PR

This is hardware-specific code (OV02C10 sensor / Intel IPU6) that CI can't
meaningfully exercise — CI only lints, byte-compiles, and checks that
GStreamer imports. Actually test your change on real hardware and note
what you checked in the PR description.
