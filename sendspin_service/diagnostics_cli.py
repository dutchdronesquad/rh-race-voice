"""Manage race-day recordings without importing the audio runtime."""

# ruff: noqa: T201, TRY301 -- command-line output and one error boundary

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .cli import ENV_FILE, _health_url, _local_url

MAX_EXPORT_BYTES = 80 * 1024 * 1024


def _token(env_file: Path) -> str:
    value = os.environ.get("SENDSPIN_API_TOKEN")
    if value is not None:
        return value.strip()
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key == "SENDSPIN_API_TOKEN":
                words = shlex.split(value, comments=True)
                return words[0] if words else ""
    return ""


def main(argv: list[str]) -> int:
    """Inspect or download the automatic recording from the running service."""
    parser = argparse.ArgumentParser(prog="sendspin-service diagnostics")
    parser.add_argument("action", choices=("status", "export"))
    parser.add_argument(
        "--url", help="Service API base URL; defaults to local configuration"
    )
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Export path; default: race-audio-YYYY-MM-DD-HHMMSSZ.zip "
            "in current directory (UTC)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        parts = urlsplit(_health_url(args.url or _local_url(args.env_file)))
        if parts.username is not None or parts.password is not None:
            raise ValueError("Use SENDSPIN_API_TOKEN instead of credentials in the URL")
        url = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path.removesuffix("/health") + f"/v2/diagnostics/{args.action}",
                "",
                "",
            )
        )
        token = _token(args.env_file)
        request = Request(  # noqa: S310 -- scheme checked by _health_url
            url,
            method="GET",
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        with urlopen(request, timeout=20) as response:  # noqa: S310
            content_type = response.headers.get_content_type()
            data = response.read(MAX_EXPORT_BYTES + 1)
        if len(data) > MAX_EXPORT_BYTES:
            raise ValueError("Diagnostic response exceeds the download limit")
        if args.action == "export":
            if content_type != "application/zip" or not data.startswith(b"PK"):
                raise ValueError("Service did not return a diagnostic ZIP")
            output_path = args.output or Path.cwd() / datetime.now(UTC).strftime(
                "race-audio-%Y-%m-%d-%H%M%SZ.zip"
            )
            with output_path.open("xb") as output:
                output.write(data)
            print(f"Saved diagnostic recording to {output_path}")
        else:
            print(json.dumps(json.loads(data), indent=2))
    except (OSError, ValueError) as exc:
        print(f"Diagnostics failed: {exc}", file=sys.stderr)
        return 1
    return 0
