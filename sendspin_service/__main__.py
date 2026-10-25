"""Command-line entry point for the Sendspin service."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch management commands before importing the audio/server runtime."""
    args = list(argv) if argv is not None else sys.argv[1:]
    if args[:1] == ["relay"]:
        from .relay_cli import main as relay_main  # noqa: PLC0415

        return relay_main(args[1:])
    from .server import main as server_main  # noqa: PLC0415

    return server_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
