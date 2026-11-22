"""Inspect and update a service without importing or starting its audio runtime."""

# ruff: noqa: T201 -- CLI output is intentionally written to stdout/stderr.

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

ENV_FILE = Path("/etc/default/sendspin-service")
INSTALLER = Path("/opt/sendspin-service/bin/install-sendspin-service.sh")
_MAX_HEALTH_BYTES = 65536


def installed_versions() -> dict[str, str]:
    """Read this executable's release and dependencies without importing them."""
    result = {"service": os.environ.get("SENDSPIN_SERVICE_VERSION", "0.0.0+dev")}
    for package in ("aiosendspin", "piper-tts"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = "not installed"
    result["python"] = sys.version.split()[0]
    return result


def _version_text() -> str:
    versions = installed_versions()
    return "\n".join(f"{name}: {value}" for name, value in versions.items())


def main(argv: list[str]) -> int:
    """Show help by default; require an explicit command for any action."""
    parser = argparse.ArgumentParser(
        prog="sendspin-service",
        description="Run, inspect and update the Sendspin playback service.",
        epilog="Use 'sendspin-service COMMAND --help' for command options.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=_version_text())
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Start the service in the foreground")
    status = commands.add_parser("status", help="Query the running service")
    status.add_argument("--url", help="Service API base URL; defaults to local config")
    status.add_argument("--env-file", type=Path, default=ENV_FILE)
    status.add_argument(
        "--json", action="store_true", help="Print machine-readable status"
    )
    commands.add_parser(
        "version", help="Show installed service and dependency versions"
    )
    update = commands.add_parser(
        "update", help="Update the Raspberry Pi/Debian .deb installation"
    )
    update.add_argument(
        "release", nargs="?", help="Exact release tag; defaults to latest stable"
    )
    update.add_argument(
        "--yes", "-y", action="store_true", help="Confirm the update without prompting"
    )
    commands.add_parser("relay", help="Configure the cloud relay")
    commands.add_parser("diagnostics", help="Capture and export race-audio diagnostics")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "version":
        print(_version_text())
        return 0
    if args.command == "status":
        return _status(args)
    if args.command == "update":
        return _update(args)
    return 0


def _local_url(env_file: Path) -> str:
    """Read the API bind address as data, without sourcing the service env file."""
    config = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key in {"SENDSPIN_INGEST_HOST", "SENDSPIN_INGEST_PORT"}:
                words = shlex.split(value, comments=True)
                if words:
                    config[key] = words[0]
    host = os.environ.get(
        "SENDSPIN_INGEST_HOST", config.get("SENDSPIN_INGEST_HOST", "127.0.0.1")
    )
    port = os.environ.get(
        "SENDSPIN_INGEST_PORT", config.get("SENDSPIN_INGEST_PORT", "8766")
    )
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)  # noqa: S104 -- map wildcard binds to loopback
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{int(port)}/"


def _health_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Service URL must start with http:// or https://")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path.rstrip("/") + "/health", "", "")
    )


def _read_health(url: str) -> dict:
    with urlopen(url, timeout=3) as response:  # noqa: S310 -- HTTP(S) checked by _health_url
        data = response.read(_MAX_HEALTH_BYTES + 1)
    if len(data) > _MAX_HEALTH_BYTES:
        raise ValueError("Health response is too large")
    health = json.loads(data)
    if not isinstance(health, dict) or health.get("ok") is not True:
        raise ValueError("Endpoint did not report a healthy Sendspin service")
    return health


def _status(args: argparse.Namespace) -> int:
    result = {"installed": installed_versions(), "reachable": False}
    try:
        result["url"] = _health_url(args.url or _local_url(args.env_file))
        health = _read_health(result["url"])
        result.update(reachable=True, running=health)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Service: {'running' if result['reachable'] else 'unreachable'}")
        print(f"API: {result.get('url', 'unknown')}")
        print(f"Installed service: {result['installed']['service']}")
        if result["reachable"]:
            print(f"Running service: {health.get('version', 'unknown')}")
            print(
                f"Running aiosendspin: {health.get('aiosendspin_version', 'unknown')}"
            )
            print(f"Connected players: {health.get('connected_players', 'unknown')}")
        else:
            print(f"Error: {result['error']}")
            print("For systemd status: systemctl status sendspin-service --no-pager")
    return 0 if result["reachable"] else 1


def _update(args: argparse.Namespace) -> int:
    """Reuse the bundled installer, including its confirmation and checksum checks."""
    if (
        args.release
        and not args.release[:1].isdigit()
        and not (args.release.startswith("v") and args.release[1:2].isdigit())
    ):
        print(
            "Use a release tag such as v2.1.0, or omit it for latest stable.",
            file=sys.stderr,
        )
        return 2
    if not INSTALLER.is_file():
        print(
            "Update requires a .deb installation on 64-bit Raspberry Pi OS or Debian. "
            "Update Docker installations by pulling and recreating the container.",
            file=sys.stderr,
        )
        return 1
    command = ["bash", str(INSTALLER), args.release or "--latest"]
    if args.yes:
        command.append("--yes")
    # The bundled launcher sets these for its private Python runtime. apt and
    # host utilities must use their own Python installation during an update.
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    try:
        return subprocess.run(command, env=environment, check=False).returncode  # noqa: S603 -- fixed local installer; release validated by installer
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"Cannot run updater: {exc}", file=sys.stderr)
        return 1
