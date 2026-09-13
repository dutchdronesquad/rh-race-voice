# Debian Package Files

This directory contains Debian-specific package assets for `sendspin-service`.

The `.deb` package is the recommended installation for use with the RotorHazard plugin: Sendspin runs as a standalone systemd service on the same Raspberry Pi OS machine as RotorHazard. It bundles its own Python runtime and dependencies. Cloud hosting is available as an optional extra through the [Docker Compose deployment](../../docs/usage.md#docker-image).

Package metadata and file mapping live in `packaging/nfpm.yaml`. The build script stages a bundled CPython runtime, service dependencies, service code, and launcher under `build/sendspin-service/`, then calls `nfpm`.

The Debian package is headless. It installs the service runtime, local HTTP ingest API, and Sendspin endpoint; it does not install the RotorHazard plugin or browser player assets.

Build:

```shell
python -m tools.build_sendspin_service_deb
```

Local builds require `uv`, `nfpm`, and Python 3.11+ for the build script.

CI/release builds:

- `.github/actions/build-sendspin-deb/action.yaml` installs `uv` and `nfpm`, stages the bundle, and packages the `.deb`.
- `.github/workflows/build.yaml` builds the `amd64` package on pull requests that touch service/package inputs.
- `.github/workflows/release.yaml` builds `amd64` and `arm64` packages on published GitHub Releases and uploads them as release assets with `.sha256` checksum files.

Target install layout:

```text
/opt/sendspin-service/runtime/
/opt/sendspin-service/app/
/opt/sendspin-service/bin/sendspin-service
/etc/default/sendspin-service
/lib/systemd/system/sendspin-service.service
```

Expected package behavior:

- install package name `sendspin-service`
- enable and restart `sendspin-service` after install/upgrade
- preserve `/etc/default/sendspin-service` on upgrade
- stop and disable the service on remove
- remove config and systemd state on purge
