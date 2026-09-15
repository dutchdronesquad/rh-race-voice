"""Configure this installation's relay without hand-editing its env file."""

from __future__ import annotations

import argparse
import secrets
import shutil
import subprocess
from pathlib import Path

from rich.console import Console
from rich.table import Table

DEFAULT_ENV_FILE = Path("/etc/default/sendspin-service")
SERVICE_NAME = "sendspin-service"
_URL_KEY = "SENDSPIN_RELAY_URL"
_TOKEN_KEY = "SENDSPIN_RELAY_TOKEN"  # noqa: S105 -- env var name, not a secret
_TOKEN_MASK_CHARS = 6

console = Console()


def main(argv: list[str]) -> int:
    """Run the requested relay subcommand."""
    args = _parse_args(argv)
    env_file = Path(args.env_file)
    lines = _read_lines(env_file)

    if args.action == "status":
        _print_status(lines)
        return 0

    if args.action == "disable":
        _write_lines(env_file, _apply(lines, {_URL_KEY: ""}))
        console.print("[green]✓[/green] Relay disabled")
    else:
        token = (
            args.token
            or (None if args.rotate_token else _existing_value(lines, _TOKEN_KEY))
            or secrets.token_hex(32)
        )
        _write_lines(env_file, _apply(lines, {_URL_KEY: args.url, _TOKEN_KEY: token}))
        console.print(f"[green]✓[/green] Relay enabled: [bold]{args.url}[/bold]")
        console.print(
            "  Copy into the cloud instance's .env as "
            "[bold]SENDSPIN_RELAY_TOKEN[/bold]:"
        )
        console.print(f"  [yellow]{token}[/yellow]")

    if not args.no_restart:
        _restart_service()
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sendspin-service relay",
        description="Configure this installation's relay to a cloud instance",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    status = subparsers.add_parser("status", help="Show the current relay config")
    _add_env_file_arg(status)

    enable = subparsers.add_parser("enable", help="Turn the relay on")
    enable.add_argument("--url", required=True, help="Cloud instance base URL")
    enable.add_argument("--token", help="Relay token; generated if omitted")
    enable.add_argument(
        "--rotate-token",
        action="store_true",
        help="Generate a fresh token even if one is already stored",
    )
    _add_env_file_arg(enable)
    _add_no_restart_arg(enable)

    disable = subparsers.add_parser("disable", help="Turn the relay off")
    _add_env_file_arg(disable)
    _add_no_restart_arg(disable)

    return parser.parse_args(argv)


def _add_env_file_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help=f"Service env file to edit, defaults to {DEFAULT_ENV_FILE}",
    )


def _add_no_restart_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart the service after writing the config",
    )


def _existing_value(lines: list[str], key: str) -> str | None:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped[len(key) + 1 :]
    return None


def _apply(lines: list[str], updates: dict[str, str]) -> list[str]:
    remaining = dict(updates)
    result = []
    for line in lines:
        stripped = line.strip()
        matched_key = next(
            (
                key
                for key in remaining
                if stripped == f"{key}=" or stripped.startswith(f"{key}=")
            ),
            None,
        )
        if matched_key is None and stripped.startswith("#"):
            uncommented = stripped.lstrip("#").strip()
            matched_key = next(
                (key for key in remaining if uncommented.startswith(f"{key}=")),
                None,
            )
        if matched_key is not None:
            result.append(f"{matched_key}={remaining.pop(matched_key)}")
        else:
            result.append(line)
    for key, value in remaining.items():
        result.append(f"{key}={value}")
    return result


def _print_status(lines: list[str]) -> None:
    url = _existing_value(lines, _URL_KEY)
    token = _existing_value(lines, _TOKEN_KEY)
    table = Table(show_header=False, box=None, padding=(0, 1, 0, 0))
    relay = f"[green]enabled[/green], {url}" if url else "[dim]disabled[/dim]"
    table.add_row("Relay", relay)
    table.add_row("Token", _mask(token))
    console.print(table)


def _mask(token: str | None) -> str:
    if not token:
        return "[dim](not set)[/dim]"
    return f"{token[:_TOKEN_MASK_CHARS]}…"


def _read_lines(env_file: Path) -> list[str]:
    if not env_file.exists():
        return []
    return env_file.read_text().splitlines()


def _write_lines(env_file: Path, lines: list[str]) -> None:
    content = "\n".join(lines) + "\n"
    tmp_path = env_file.with_suffix(env_file.suffix + ".tmp")
    tmp_path.write_text(content)
    tmp_path.replace(env_file)


def _restart_service() -> None:
    if shutil.which("systemctl") is None:
        console.print(
            f"[yellow]![/yellow] systemctl not found; "
            f"restart {SERVICE_NAME} manually to apply."
        )
        return
    subprocess.run(["systemctl", "restart", SERVICE_NAME], check=True)  # noqa: S603, S607
