"""Bounded, content-addressed audio reuse for remote producers."""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from .audio_queue import WavItem

_CACHE_BYTES = 64 * 1024 * 1024
_MAX_UPLOADS = 2048


class MissingAudioError(ValueError):
    """Report missing references before any playback is queued."""

    def __init__(self, hashes: list[str]) -> None:
        """Retain the references the producer must upload again."""
        super().__init__("audio is no longer cached")
        self.hashes = hashes


class AudioCache:
    """Reuse bundled assets and keep recent uploads within a memory budget."""

    def __init__(self, asset_dir: Path, max_bytes: int = _CACHE_BYTES) -> None:
        """Index trusted packaged assets and initialize the upload LRU."""
        self._assets = {}
        for path in asset_dir.glob("*.wav"):
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            self._assets[digest] = path
        self._uploads: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    @property
    def bundled_hashes(self) -> list[str]:
        """Advertise immutable assets already present on the service."""
        return list(self._assets)

    def resolve(
        self, references: Any, uploads: list[WavItem]
    ) -> tuple[list[WavItem], list[str]]:
        """Resolve a complete job atomically, or reject it without partial playback."""
        if not isinstance(references, list) or not references:
            raise ValueError("wav_refs must be a non-empty list")
        _validate_references(references)
        uploaded = {
            hashlib.sha256(item.data).hexdigest(): item.data
            for item in uploads
            if item.data is not None
        }
        with self._lock:
            missing = [
                ref["sha256"]
                for ref in references
                if ref["sha256"] not in self._assets
                and ref["sha256"] not in uploaded
                and ref["sha256"] not in self._uploads
            ]
            if missing:
                raise MissingAudioError(missing)
            items = []
            for ref in references:
                digest = ref["sha256"]
                if digest in self._assets:
                    items.append(
                        WavItem(name=ref["name"], path=str(self._assets[digest]))
                    )
                else:
                    data = uploaded.get(digest, self._uploads.get(digest))
                    items.append(WavItem(name=ref["name"], data=data))
                    if digest in self._uploads:
                        self._uploads.move_to_end(digest)
            for digest, data in uploaded.items():
                if digest in self._assets or digest in self._uploads:
                    continue
                if len(data) > self._max_bytes:
                    continue
                while (
                    self._size + len(data) > self._max_bytes
                    or len(self._uploads) >= _MAX_UPLOADS
                ):
                    _, evicted = self._uploads.popitem(last=False)
                    self._size -= len(evicted)
                self._uploads[digest] = data
                self._size += len(data)
            cached = [
                ref["sha256"]
                for ref in references
                if ref["sha256"] in self._assets or ref["sha256"] in self._uploads
            ]
        return items, cached


def _validate_references(references: list) -> None:
    for ref in references:
        if (
            not isinstance(ref, dict)
            or not isinstance(ref.get("name"), str)
            or not isinstance(ref.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])
        ):
            raise ValueError("invalid audio reference")
