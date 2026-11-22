"""Bounded, opt-in race-audio capture; disk work never runs on an audio callback."""

from __future__ import annotations

import io
import json
import logging
import math
import os
import platform
import queue
import threading
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from .race import telemetry

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_FILE_BYTES = 1024 * 1024
BACKUP_COUNT = 4
MAX_QUEUE_RECORDS = 2048
_README = """Race Voice audio diagnostics

manifest.json identifies the capture, software, hardware and clock domain.
summary.json contains counts and timing distributions from retained events only.
events.jsonl.N files are oldest first (largest N), followed by events.jsonl.

output_played/delivery=queued means accepted by an output, not heard on a speaker.
sendspin_schedule records planned starts; target_late_ms is scheduled tone lateness.
sendspin_chunks records timestamp gaps and scheduling lead in the SDK timeline.
A scheduled silence between tones is expected; a chunk gap inside a clip is different.
Queue waits can overlap synthesis: do not add every duration to estimate audible delay.
Never subtract timestamps with different clock_id values, including separate services.

Start a browser recording under Diagnostics and download its JSON after an issue.
The browser file is separate; it contains no service token or stable player identity.
Buffer/resync observations do not prove an underrun or physical sound output.
WindowsSpin does not send authoritative speaker/underrun measurements to this service.

Storage and queue loss are reported in capture_status. Older events may have rotated
out, so an unmatched output is not evidence that its receive/preparation stage failed.
"""


def _device_model() -> str | None:
    try:
        with Path("/sys/firmware/devicetree/base/model").open("rb") as source:
            return source.read(256).decode("utf-8", errors="replace").rstrip("\x00\n")
    except OSError:
        return None


_FIELDS = frozenset(
    [
        "event_id",
        "stage",
        "t",
        "clock_id",
        "kind",
        "origin",
        "destination",
        "reason",
        "outcome",
        "delivery",
        "pilot_id",
        "lap",
        "heat_id",
        "generation",
        "model",
        "speed",
        "noise",
        "noise_w",
        "asset",
        "wait_ms",
        "duration_ms",
        "cache_hit",
        "memory_cache_hit",
        "empty",
        "segment_index",
        "subdir",
        "worker_wait_ms",
        "request_ms",
        "elapsed_ms",
        "pending",
        "depth",
        "bytes",
        "attempts",
        "upload_ms",
        "delivery_ms",
        "missing_assets",
        "upload_bytes",
        "start_in_ms",
        "target_late_ms",
        "audio_ms",
        "client_count",
        "schedule_gap_ms",
        "chunk_gap_ms",
        "min_lead_ms",
        "max_buffer_ms",
        "scheduled_start",
        "scheduled_end",
        "connected",
        "client_product",
        "client_software",
    ]
)


def _percentiles(values: list[float]) -> dict:
    values.sort()
    count = len(values)
    return {
        "samples": count,
        "max_ms": values[-1],
        **{
            name: values[min(count - 1, math.ceil(count * percentile) - 1)]
            if count >= minimum
            else None
            for name, percentile, minimum in (
                ("p50_ms", 0.5, 1),
                ("p95_ms", 0.95, 20),
                ("p99_ms", 0.99, 100),
            )
        },
    }


def summarize(lines: Iterable[str]) -> dict:
    """Group clocks separately and report queue acceptance as queue acceptance."""
    counts: Counter = Counter()
    metrics: dict[str, list[float]] = defaultdict(list)
    received: dict[tuple, float] = {}
    invalid = 0
    for line in lines:
        try:
            item = json.loads(line)
            clock, stage = item["clock_id"], item["stage"]
            key = (clock, item["event_id"])
            stamp = item["t"]
            group = f"{clock}/{item.get('destination', 'preparation')}/{stage}"
            counts[group] += 1
            if stage == "received":
                received.setdefault(key, stamp)
            if stage in {"output_dropped", "output_filtered"}:
                counts[f"{group}/{item.get('reason', 'unknown')}"] += 1
            for name, value in item.items():
                if (
                    name.endswith("_ms")
                    and type(value) in (float, int)
                    and math.isfinite(value)
                ):
                    metrics[f"{group}/{name}"].append(value)
            if stage == "output_played" and key in received:
                metrics[f"{group}/receipt_to_queue_ms"].append(
                    round((stamp - received[key]) * 1000, 3)
                )
        except (ValueError, KeyError, TypeError):
            invalid += 1
    return {
        "counts": dict(counts),
        "metrics": {name: _percentiles(values) for name, values in metrics.items()},
        "invalid_records": invalid,
        "delivery_meaning": "output_played means queued, not audible output",
        "clock_rule": "Durations are compared only within the same clock_id",
        "percentile_min_samples": {"p50": 1, "p95": 20, "p99": 100},
    }


class _Writer(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802, ARG002
        """Report storage failures to the writer without logging recursively."""
        raise  # noqa: PLE0704 -- called from Handler.emit's exception handler


class _CaptureHandler(logging.Handler):
    def __init__(self, owner: DiagnosticCapture) -> None:
        super().__init__(logging.INFO)
        self.owner = owner

    def emit(self, record: logging.LogRecord) -> None:
        """Sanitize and enqueue without waiting for the filesystem or writer."""
        try:
            item = json.loads(record.getMessage())
            safe = {
                key: value
                for key, value in item.items()
                if key in _FIELDS
                and (
                    value is None
                    or type(value) in (bool, int, float)
                    or (isinstance(value, str) and len(value) <= 256)
                )
            }
            line = json.dumps(safe, allow_nan=False)
            if len(line) > 4096 or self.owner.error:
                self.owner.dropped += 1
                return
            self.owner.records.put_nowait(line)
        except (ValueError, TypeError, AttributeError, queue.Full):
            self.owner.dropped += 1


class DiagnosticCapture:
    """Own one rotating capture with a bounded producer queue and explicit lifecycle."""

    def __init__(
        self, directory: Path, metadata: dict, *, max_bytes: int = MAX_FILE_BYTES
    ) -> None:
        """Keep file I/O in the off-loop start/export/stop operations."""
        if max_bytes < 4096:
            raise ValueError("Diagnostic files must hold at least one bounded record")
        self.directory = directory
        self.metadata = {
            key: value
            for key, value in metadata.items()
            if key in {"versions", "local_output", "relay_configured", "relay_receiver"}
        }
        self.max_bytes = max_bytes
        self.records: queue.Queue[str | threading.Event | None] = queue.Queue(
            MAX_QUEUE_RECORDS
        )
        self.dropped = 0
        self.written = 0
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._handler: _CaptureHandler | None = None
        self._file_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._manifest: dict = {}
        self._previous_level = logging.NOTSET

    def status(self) -> dict:
        """Return cheap operational state without inspecting files or audio clients."""
        return {
            "active": self._handler is not None,
            "capture_id": self._manifest.get("capture_id"),
            "written_records": self.written,
            "dropped_records": self.dropped,
            "queued_records": self.records.qsize(),
            "storage_error": self.error,
            "max_file_bytes": self.max_bytes,
            "max_files": BACKUP_COUNT + 1,
            "max_queue_records": MAX_QUEUE_RECORDS,
        }

    def start(self) -> dict:
        """Begin a new capture, replacing only the previous diagnostic recording."""
        with self._control_lock:
            if self._handler is not None:
                return self.status()
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Previous diagnostic writer is still stopping")
            self.directory.mkdir(parents=True, exist_ok=True)
            for name in ("events.jsonl", *(f"events.jsonl.{i}" for i in range(1, 5))):
                (self.directory / name).unlink(missing_ok=True)
            writer = _Writer(
                self.directory / "events.jsonl",
                maxBytes=self.max_bytes,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            self.records = queue.Queue(MAX_QUEUE_RECORDS)
            self.dropped = self.written = 0
            self.error = None
            self._manifest = {
                "schema": "race-audio-diagnostics/1",
                "capture_id": uuid.uuid4().hex,
                "clock_id": telemetry.CLOCK_ID,
                "started_utc": datetime.now(UTC).isoformat(),
                "started_monotonic": time.monotonic(),
                "architecture": platform.machine(),
                "system": platform.system(),
                "device_model": _device_model(),
                "logical_cpus": os.cpu_count(),
                "python": platform.python_version(),
                **self.metadata,
            }
            try:
                self._save_manifest()
            except OSError:
                writer.close()
                raise
            self._thread = threading.Thread(
                target=self._write,
                args=(writer,),
                name="race-audio-diagnostics",
                daemon=True,
            )
            self._thread.start()
            self._previous_level = telemetry.logger.level
            if not telemetry.logger.isEnabledFor(logging.INFO):
                telemetry.logger.setLevel(logging.INFO)
            self._handler = _CaptureHandler(self)
            telemetry.logger.addHandler(self._handler)
            return self.status()

    def stop(self) -> dict:
        """Detach immediately, then drain the bounded queue with a bounded join."""
        with self._control_lock:
            if self._handler is None:
                return self.status()
            telemetry.logger.removeHandler(self._handler)
            self._handler.close()
            self._handler = None
            if telemetry.logger.level == logging.INFO:
                telemetry.logger.setLevel(self._previous_level)
            try:
                self.records.put(None, timeout=5)
                if self._thread is not None:
                    self._thread.join(timeout=5)
                    if self._thread.is_alive():
                        self.error = "writer_timeout"
            except queue.Full:
                self.error = "writer_timeout"
            self._manifest["stopped_utc"] = datetime.now(UTC).isoformat()
            try:
                self._save_manifest()
            except OSError as exc:
                self.error = type(exc).__name__
            return self.status()

    def _write(self, writer: _Writer) -> None:
        try:
            while True:
                line = self.records.get()
                try:
                    if line is None:
                        return
                    if isinstance(line, threading.Event):
                        line.set()
                        continue
                    if self.error:
                        self.dropped += 1
                        continue
                    with self._file_lock:
                        writer.emit(logging.makeLogRecord({"msg": line}))
                    self.written += 1
                except OSError as exc:
                    self.error = type(exc).__name__
                    self.dropped += 1
                finally:
                    self.records.task_done()
        finally:
            writer.close()

    def _save_manifest(self) -> None:
        self._manifest["capture_status"] = self.status()
        temporary = self.directory / ".manifest.tmp"
        temporary.write_text(json.dumps(self._manifest, indent=2), encoding="utf-8")
        temporary.replace(self.directory / "manifest.json")

    def export(self) -> bytes:
        """Snapshot retained files off-loop without stopping an active capture."""
        with self._control_lock:
            if self._handler is not None:
                flushed = threading.Event()
                try:
                    self.records.put(flushed, timeout=3)
                except queue.Full as exc:
                    raise RuntimeError("Diagnostic writer is busy") from exc
                if not flushed.wait(timeout=3):
                    raise RuntimeError("Diagnostic writer is busy")
            if not self._file_lock.acquire(timeout=3):
                raise RuntimeError("Diagnostic storage is busy")
            try:
                manifest = self._manifest or json.loads(
                    (self.directory / "manifest.json").read_text(encoding="utf-8")
                )
                manifest = {**manifest, "exported_utc": datetime.now(UTC).isoformat()}
                manifest["recording_active"] = self._handler is not None
                if self._manifest:
                    manifest["capture_status"] = self.status()
                files = [
                    path
                    for index in range(BACKUP_COUNT, -1, -1)
                    if (
                        path := self.directory
                        / (f"events.jsonl.{index}" if index else "events.jsonl")
                    ).is_file()
                    and not path.is_symlink()
                ]
                buffer = io.BytesIO()
                records = []
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path in files:
                        data = path.read_bytes()
                        archive.writestr(path.name, data)
                        records.extend(
                            data.decode("utf-8", errors="replace").splitlines()
                        )
                    archive.writestr("manifest.json", json.dumps(manifest, indent=2))
                    archive.writestr("README.txt", _README)
                    archive.writestr(
                        "summary.json", json.dumps(summarize(records), indent=2)
                    )
                return buffer.getvalue()
            finally:
                self._file_lock.release()
