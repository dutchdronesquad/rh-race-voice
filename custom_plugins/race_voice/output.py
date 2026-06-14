"""Output adapters for Race Voice playback."""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from .audio_queue import Priority

logger = logging.getLogger(__name__)


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

    def play(  # noqa: PLR0913
        self,
        text: str,
        wav_paths: list[Path],
        priority: Priority,
        expires_at: float | None = None,
        play_at: float | None = None,
        volume: float = 1.0,
    ) -> None:
        """Send WAV files to the service as inline base64 payloads."""
        if expires_at is not None and time.monotonic() > expires_at:
            logger.info("Race Voice dropped stale service audio: '%s'", text)
            return
        wav_files = self._wav_files(wav_paths)
        if not wav_files:
            logger.warning("Race Voice: no readable WAV files for Sendspin service")
            return
        payload: dict[str, Any] = {
            "text": text,
            "wav_files": wav_files,
            "priority": priority.name.lower(),
            "volume": volume,
        }
        now = time.monotonic()
        if expires_at is not None:
            payload["expiry_sec"] = max(0.0, expires_at - now)
        if play_at is not None:
            payload["play_at_delay_sec"] = max(0.0, play_at - now)
        self._post_json("/v1/play", payload)

    def stop(self) -> None:
        """Stop service playback and clear queued service audio."""
        self._post_json("/v1/stop", {})

    def service_version(self) -> str | None:
        """Return the service version from /health, if available."""
        try:
            base_url = self._base_url()
            request = urllib.request.Request(  # noqa: S310
                f"{base_url}/health",
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=self._timeout_s()) as response:  # noqa: S310
                payload = json.load(response)
        except (TimeoutError, ValueError, json.JSONDecodeError, urllib.error.URLError):
            logger.debug("Race Voice: cannot read Sendspin service version")
            return None
        if not isinstance(payload, dict):
            return None
        version = payload.get("version")
        return str(version).strip() if version else None

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

    def _post_json(self, path: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            base_url = self._base_url()
            request = urllib.request.Request(  # noqa: S310
                f"{base_url}{path}",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self._timeout_s()) as response:  # noqa: S310
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
