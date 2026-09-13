"""Output adapters for Race Voice playback."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
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
        api_token: Callable[[], str] | None = None,
        enabled: Callable[[], bool] | None = None,
        reuse_audio: bool = False,
    ) -> None:
        """Configure lazy option lookups for each request."""
        self._service_url = service_url
        self._timeout_s = timeout_s
        self._api_token = api_token
        self._enabled = enabled
        self._reuse_audio = reuse_audio
        self._reference_support: tuple[str, bool, set[str]] | None = None
        self._bundled_audio: set[str] = set()
        self._fingerprints: OrderedDict[tuple, str] = OrderedDict()
        self._multipart_support: tuple[str, bool] | None = None

    def health(self) -> dict[str, Any]:
        """Read the running service's metadata, propagating request failures."""
        base_url = self._base_url()
        self._multipart_support = None
        request = urllib.request.Request(  # noqa: S310
            f"{base_url}/health",
            headers=self._request_headers(),
        )
        with urllib.request.urlopen(request, timeout=self._timeout_s()) as response:  # noqa: S310
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise TypeError("Sendspin service health response must be a JSON object")
        self._multipart_support = (
            base_url,
            payload.get("supports_multipart_play") is True,
        )
        known = set()
        if (
            self._reference_support is not None
            and self._reference_support[0] == base_url
        ):
            known.update(self._reference_support[2])
        self._bundled_audio = _audio_hashes(payload.get("bundled_audio"))
        known.update(self._bundled_audio)
        self._reference_support = (
            base_url,
            payload.get("supports_audio_references") is True,
            known,
        )
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
        if self._enabled is not None and not self._enabled():
            return
        if expires_at is not None and time.monotonic() > expires_at:
            logger.info("Race Voice dropped stale service audio: '%s'", text)
            return
        if self._reuse_audio and self._references_available():
            try:
                self._play_references(
                    text, wav_paths, priority, expires_at, play_at, volume
                )
            except (OSError, ValueError):
                logger.exception("Race Voice: cannot prepare cached cloud audio")
            return
        multipart = self._use_multipart(wav_paths)
        wav_files = [] if multipart else self._wav_files(wav_paths)
        if not multipart and not wav_files:
            logger.warning("Race Voice: no readable WAV files for Sendspin service")
            return
        payload: dict[str, Any] = {
            "text": text,
            "priority": _wire_priority(priority.name),
            "kind": {"SIGNAL": "race_signal", "LAP": "lap"}.get(priority.name, "voice"),
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

    def _references_available(self) -> bool:
        try:
            base_url = self._base_url()
            if (
                self._reference_support is None
                or self._reference_support[0] != base_url
            ):
                self.health()
            return self._reference_support is not None and self._reference_support[1]
        except (OSError, ValueError, TypeError):
            return False

    def _fingerprint(self, path: Path) -> str:
        stat = path.stat()
        key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if key not in self._fingerprints:
            with path.open("rb") as file:
                self._fingerprints[key] = hashlib.file_digest(
                    file, "sha256"
                ).hexdigest()
        self._fingerprints.move_to_end(key)
        while len(self._fingerprints) > 256:
            self._fingerprints.popitem(last=False)
        return self._fingerprints[key]

    def _play_references(  # noqa: PLR0913
        self,
        text: str,
        wav_paths: list[Path],
        priority: Priority,
        expires_at: float | None,
        play_at: float | None,
        volume: float,
    ) -> None:
        if self._reference_support is None:
            return
        base_url, _, known = self._reference_support
        references = [
            {"name": path.name, "sha256": self._fingerprint(path)} for path in wav_paths
        ]
        for attempt in range(2):
            now = time.monotonic()
            if expires_at is not None and now > expires_at:
                return
            pending = [
                path
                for path, ref in zip(wav_paths, references, strict=True)
                if ref["sha256"] not in known
            ]
            payload: dict[str, Any] = {
                "text": text,
                "priority": _wire_priority(priority.name),
                "kind": {"SIGNAL": "race_signal", "LAP": "lap"}.get(
                    priority.name, "voice"
                ),
                "volume": volume,
                "wav_refs": references,
            }
            if expires_at is not None:
                payload["expiry_sec"] = max(0.0, expires_at - now)
            if play_at is not None:
                payload["play_at_delay_sec"] = max(0.0, play_at - now)
            multipart = bool(pending) and self._use_multipart(pending)
            if not multipart:
                payload["wav_files"] = self._wav_files(pending)
            result = self._post_json(
                "/v1/play",
                payload,
                wav_paths=pending if multipart else None,
                base_url=base_url,
                read_response=True,
            )
            if result is None:
                return
            missing = _audio_hashes(result.get("missing_audio"))
            if missing:
                known.difference_update(missing)
                if attempt == 0:
                    continue
                logger.error("Race Voice: cloud audio references still unavailable")
                return
            self._remember_audio(known, references, result)
            return

    def _remember_audio(
        self, known: set[str], references: list[dict[str, str]], result: dict[str, Any]
    ) -> None:
        known.difference_update(ref["sha256"] for ref in references)
        retained = _audio_hashes(result.get("cached_audio"))
        known.update(retained)
        if len(known) > 4096:
            known.intersection_update(self._bundled_audio | retained)

    def _use_multipart(self, wav_paths: list[Path]) -> bool:
        """Negotiate raw uploads for large clips, retaining the older JSON API."""
        try:
            if (
                sum(path.stat().st_size for path in wav_paths)
                < _MULTIPART_THRESHOLD_BYTES
            ):
                return False
            if (
                self._multipart_support is not None
                and self._multipart_support[0] == self._base_url()
            ):
                return self._multipart_support[1]
            return self.health().get("supports_multipart_play") is True
        except (OSError, ValueError, TypeError):
            logger.debug("Race Voice: raw upload capability unavailable", exc_info=True)
            return False

    def stop(self) -> None:
        """Stop service playback and clear queued service audio."""
        if self._enabled is not None and not self._enabled():
            return
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
        base_url: str | None = None,
        read_response: bool = False,
    ) -> dict[str, Any] | None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            base_url = base_url or self._base_url()
            with contextlib.ExitStack() as stack:
                headers = {"Content-Type": "application/json"}
                body: bytes | Iterator[bytes] = data
                if wav_paths is not None:
                    body, headers = _multipart_body(stack, data, wav_paths)
                headers.update(self._request_headers())
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
                if read_response:
                    result = json.load(response)
                    return _response_object(result)
        except urllib.error.HTTPError as exc:
            return self._http_error_result(
                exc, base_url, path, read_response=read_response
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
        except (ValueError, TypeError):
            logger.exception("Race Voice: invalid Sendspin request or response")
        except OSError:
            logger.exception("Race Voice: Sendspin upload failed: %s", path)
        return None

    def _http_error_result(
        self,
        exc: urllib.error.HTTPError,
        base_url: str | None,
        path: str,
        *,
        read_response: bool,
    ) -> dict[str, Any] | None:
        with exc:
            error_body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 409 and read_response:
            with contextlib.suppress(ValueError):
                result = json.loads(error_body)
                if isinstance(result, dict) and _audio_hashes(
                    result.get("missing_audio")
                ):
                    return result
        if exc.code == 403 and "error code: 1010" in error_body.lower():
            logger.error(
                "Race Voice: Cloudflare blocked %s%s (403, code 1010). "
                "Check Browser Integrity Check for the cloud API hostname; "
                "configure an API-scoped exception. This request did not "
                "reach Sendspin.",
                base_url,
                path,
            )
            return None
        logger.error(
            "Race Voice: Sendspin service rejected %s (%s): %s",
            path,
            exc.code,
            error_body,
            exc_info=exc,
        )
        return None

    def _request_headers(self) -> dict[str, str]:
        token = self._api_token().strip() if self._api_token is not None else ""
        headers = {"User-Agent": "RaceVoice/1.0", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

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


def _wire_priority(name: str) -> str:
    """Retain useful priority ordering on older services without job kinds."""
    return {"SIGNAL": "high", "LAP": "low"}.get(name, name.lower())


def _response_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("invalid Sendspin response")
    return value


def _audio_hashes(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        item
        for item in value
        if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
    }


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
