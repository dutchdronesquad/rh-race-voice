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

    def test_missing_reports_only_absent_hashes(self) -> None:
        """A hash already resolved once is no longer reported missing."""
        data = b"cached"
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(self.cache.missing([digest]), [digest])
        self.cache.store(digest, data)
        self.assertEqual(self.cache.missing([digest, "0" * 64]), ["0" * 64])

    def test_store_then_get_round_trips(self) -> None:
        """A stored blob is returned unchanged by a later get()."""
        data = b"stored"
        digest = hashlib.sha256(data).hexdigest()
        self.cache.store(digest, data)
        self.assertEqual(self.cache.get(digest), data)

    def test_store_retains_an_oversized_blob_unlike_resolve(self) -> None:
        """store()/get() are separate round trips, so nothing gets silently dropped."""
        data = b"larger than the cache"
        digest = hashlib.sha256(data).hexdigest()
        self.cache.store(digest, data)
        self.assertEqual(self.cache.get(digest), data)

    def test_store_eviction_drops_the_oldest_upload(self) -> None:
        """A store() over budget evicts the least recently used entry first."""
        first, second = b"first", b"next"
        self.cache.store(hashlib.sha256(first).hexdigest(), first)
        self.cache.store(hashlib.sha256(second).hexdigest(), second)
        self.assertIsNone(self.cache.get(hashlib.sha256(first).hexdigest()))
        self.assertEqual(self.cache.get(hashlib.sha256(second).hexdigest()), second)

    def test_get_returns_none_for_an_unknown_hash(self) -> None:
        """A hash that was never bundled or uploaded resolves to nothing."""
        self.assertIsNone(self.cache.get("0" * 64))


class BundledAssetCacheTests(unittest.TestCase):
    """A relayed hash matching a bundled asset never needs uploading."""

    def test_get_reads_a_bundled_asset_from_disk(self) -> None:
        """get() serves a packaged asset's bytes by its content hash."""
        with TemporaryDirectory() as directory:
            asset_path = Path(directory) / "stage.wav"
            asset_path.write_bytes(b"bundled-tone")
            cache = AudioCache(Path(directory))
            digest = hashlib.sha256(b"bundled-tone").hexdigest()
            self.assertEqual(cache.missing([digest]), [])
            self.assertEqual(cache.get(digest), b"bundled-tone")
