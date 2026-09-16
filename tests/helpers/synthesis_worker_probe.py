"""Controlled subprocess for supervisor failure and isolation tests."""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> None:
    """Simulate blocking/crashing inference through the real pipe protocol."""
    for count, line in enumerate(sys.stdin, start=1):
        data = json.loads(line)
        if data.get("crash"):
            raise SystemExit(3)
        time.sleep(data.get("delay", 0))
        reply = {"ok": True, "result": {"pid": os.getpid(), "count": count}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
