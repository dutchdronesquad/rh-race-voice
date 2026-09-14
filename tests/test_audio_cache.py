"""Verify remote cache limits and content validation without audio hardware."""

# ruff: noqa: PT009, PT027, SLF001

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sendspin_service.playback.audio_cache import AudioCache, MissingAudioError
from sendspin_service.playback.audio_queue import WavItem


def reference(data: bytes) -> dict[str, str]:
    """Create an upload reference independently from cache implementation."""
    return {"name": "test.wav", "sha256": hashlib.sha256(data).hexdigest()}


class AudioCacheTests(unittest.TestCase):
    """Protect cache budgets and already queued clips during eviction."""

    def setUp(self) -> None:
        """Use an empty asset directory and a tiny cache budget."""
        directory = self.enterContext(TemporaryDirectory())
        self.cache = AudioCache(Path(directory), max_bytes=5)

    def test_eviction_keeps_previously_resolved_audio_valid(self) -> None:
        """Evict old entries without invalidating an already accepted job."""
        first = b"first"
        second = b"next"
        items, _ = self.cache.resolve(
            [reference(first)], [WavItem(name="first.wav", data=first)]
        )
        self.cache.resolve([reference(second)], [WavItem(name="next.wav", data=second)])
        with self.assertRaises(MissingAudioError):
            self.cache.resolve([reference(first)], [])
        self.assertEqual(items[0].data, first)
        self.assertLessEqual(self.cache._size, 5)

    def test_large_upload_can_play_without_retaining_it(self) -> None:
        """Oversized cache entries still play once without exceeding the budget."""
        data = b"larger than the cache"
        items, retained = self.cache.resolve(
            [reference(data)], [WavItem(name="large.wav", data=data)]
        )
        self.assertEqual(items[0].data, data)
        self.assertEqual(retained, [])
        self.assertEqual(self.cache._size, 0)

    def test_wrong_content_cannot_satisfy_a_reference(self) -> None:
        """Hash the actual bytes instead of trusting producer-supplied metadata."""
        with self.assertRaises(MissingAudioError):
            self.cache.resolve(
                [reference(b"expected")], [WavItem(name="test.wav", data=b"wrong")]
            )
        self.assertEqual(self.cache._size, 0)
