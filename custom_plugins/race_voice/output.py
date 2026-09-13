"""Output adapters for Race Voice playback."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from .audio_queue import Priority

logger = logging.getLogger(__name__)

_MULTIPART_THRESHOLD_BYTES = 1024 * 1024
_UPLOAD_CHUNK_BYTES = 64 * 1024


def service_version_warning(health: dict[str, Any]) -> str | None:
    """Check the backend requirement of the bundled Sendspin JS 5 player."""
    backend = health.get("aiosendspin_version")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+[\w.]+)?", str(backend))
    if match is None:
        return (
            "Race Voice cannot verify browser-player compatibility: the Sendspin "
            "service does not report a recognized aiosendspin version. "
            "Update the separate sendspin-service installation; updating the "
            "RotorHazard venv does not update that service."
        )
    if int(match[1]) < 9:
        return (
            f"Race Voice browser player requires aiosendspin 9.0.0 or newer, "
            f"but the running Sendspin service reports {backend}. "
            "Update the separate sendspin-service installation; updating the "
            "RotorHazard venv does not update that service."
        )
    return None


class SendspinServiceClient:
    """HTTP client for the standalone Sendspin service."""

    def __init__(
        self,
        *,
        service_url: Callable[[], str],
        timeout_s: Callable[[], float],
    ) -> None:
        """Configure lazy option lookups for each request."""
        self._service_url = service_url
        self._timeout_s = timeout_s

    def health(self) -> dict[str, Any]:
        """Read the running service's metadata, propagating request failures."""
        request = urllib.request.Request(  # noqa: S310
            f"{self._base_url()}/health", headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self._timeout_s()) as response:  # noqa: S310
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise TypeError("Sendspin service health response must be a JSON object")
        return payload

    def play(  # noqa: PLR0913
        self,
        text: str,
        wav_paths: list[Path],
        priority: Priority,
        expires_at: float | None = None,
        play_at: float | None = None,
        volume: float = 1.0,
    ) -> None:
        """Send WAV files using JSON or negotiated streaming multipart uploads."""
        if expires_at is not None and time.monotonic() > expires_at:
            logger.info("Race Voice dropped stale service audio: '%s'", text)
            return
        multipart = self._use_multipart(wav_paths)
        wav_files = [] if multipart else self._wav_files(wav_paths)
        if not multipart and not wav_files:
            logger.warning("Race Voice: no readable WAV files for Sendspin service")
            return
        payload: dict[str, Any] = {
            "text": text,
            "priority": priority.name.lower(),
            "volume": volume,
        }
        now = time.monotonic()
        if expires_at is not None:
            payload["expiry_sec"] = max(0.0, expires_at - now)
        if play_at is not None:
            payload["play_at_delay_sec"] = max(0.0, play_at - now)
        if multipart:
            self._post_json("/v1/play", payload, wav_paths=wav_paths)
        else:
            payload["wav_files"] = wav_files
            self._post_json("/v1/play", payload)

    def _use_multipart(self, wav_paths: list[Path]) -> bool:
        """Negotiate raw uploads for large clips, retaining the older JSON API."""
        try:
            if (
                sum(path.stat().st_size for path in wav_paths)
                < _MULTIPART_THRESHOLD_BYTES
            ):
                return False
            return self.health().get("supports_multipart_play") is True
        except (OSError, ValueError, TypeError):
            logger.debug("Race Voice: raw upload capability unavailable", exc_info=True)
            return False

    def stop(self) -> None:
        """Stop service playback and clear queued service audio."""
        self._post_json("/v1/stop", {})

    @staticmethod
    def _wav_files(wav_paths: list[Path]) -> list[dict[str, str]]:
        wav_files: list[dict[str, str]] = []
        for wav_path in wav_paths:
            try:
                data = wav_path.read_bytes()
            except OSError:
                logger.exception(
                    "Race Voice: cannot read WAV for Sendspin service: %s",
                    wav_path,
                )
                continue
            wav_files.append(
                {
                    "name": wav_path.name,
                    "encoding": "base64",
                    "data": base64.b64encode(data).decode("ascii"),
                }
            )
        return wav_files

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        wav_paths: list[Path] | None = None,
    ) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            base_url = self._base_url()
            with contextlib.ExitStack() as stack:
                headers = {"Content-Type": "application/json"}
                body: bytes | Iterator[bytes] = data
                if wav_paths is not None:
                    body, headers = _multipart_body(stack, data, wav_paths)
                request = urllib.request.Request(  # noqa: S310
                    f"{base_url}{path}", data=body, headers=headers, method="POST"
                )
                response = stack.enter_context(
                    urllib.request.urlopen(request, timeout=self._timeout_s())  # noqa: S310
                )
                if response.status >= 400:
                    logger.error(
                        "Race Voice: Sendspin service request failed: %s %s",
                        response.status,
                        path,
                    )
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            logger.exception(
                "Race Voice: Sendspin service rejected %s (%s): %s",
                path,
                exc.code,
                error_body,
            )
        except urllib.error.URLError as exc:
            logger.exception(
                "Race Voice: Sendspin service is not reachable at %s: %s",
                base_url,
                exc.reason,
            )
        except TimeoutError:
            logger.exception(
                "Race Voice: Sendspin service timed out after %.1fs: %s",
                self._timeout_s(),
                path,
            )
        except ValueError:
            logger.exception("Race Voice: invalid Sendspin service URL")
        except OSError:
            logger.exception("Race Voice: Sendspin upload failed: %s", path)

    def _base_url(self) -> str:
        url = self._service_url().strip().rstrip("/")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            scheme = parsed.scheme or "<empty>"
            message = f"invalid Sendspin service URL scheme: {scheme}"
            raise ValueError(message)
        if not parsed.netloc:
            message = "invalid Sendspin service URL: missing host"
            raise ValueError(message)
        if parsed.path or parsed.params or parsed.query or parsed.fragment:
            message = "invalid Sendspin service URL: use http(s)://host[:port]"
            raise ValueError(message)
        return url


def _multipart_body(
    stack: contextlib.ExitStack, metadata: bytes, wav_paths: list[Path]
) -> tuple[Iterator[bytes], dict[str, str]]:
    """Stream WAV files with an exact content length and bounded read buffers."""
    boundary = uuid.uuid4().hex
    prefix = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="metadata"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode()
        + metadata
        + b"\r\n"
    )
    files = []
    length = len(prefix)
    for path in wav_paths:
        file = stack.enter_context(path.open("rb"))
        size = os.fstat(file.fileno()).st_size
        name = urllib.parse.quote(path.name, safe="")
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="wav_files"; filename="{name}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        files.append((header, file, size))
        length += len(header) + size + 2
    suffix = f"--{boundary}--\r\n".encode()
    length += len(suffix)

    def chunks() -> Iterator[bytes]:
        yield prefix
        for header, file, size in files:
            yield header
            remaining = size
            while remaining:
                chunk = file.read(min(_UPLOAD_CHUNK_BYTES, remaining))
                if not chunk:
                    raise OSError("WAV file became shorter during upload")
                remaining -= len(chunk)
                yield chunk
            yield b"\r\n"
        yield suffix

    return chunks(), {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(length),
    }
