"""Cover the aiosendspin 9 server API and open playback admission."""

# Use the standard-library test runner.
# ruff: noqa: PT009, PT027, SLF001

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from aiosendspin.noise.trust_store import PskCategory

from sendspin_service.playback.sendspin import SendSpinServer, _load_identity


class SendspinStartupTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real dependency constructor without opening a network port."""

    async def test_strict_stop_reports_group_failure_after_stopping_other_groups(
        self,
    ) -> None:
        """Primary stop acknowledgement must include failures clearing client groups."""
        backend = SendSpinServer()
        failed = Mock(stop=AsyncMock(side_effect=RuntimeError("group failed")))
        healthy = Mock(stop=AsyncMock())
        backend._server = Mock(
            connected_clients=[Mock(group=failed), Mock(group=healthy)]
        )
        with self.assertRaisesRegex(RuntimeError, "group failed"):
            await backend._stop_stream(strict=True)
        healthy.stop.assert_awaited_once()
        failed.stop.assert_awaited_once()

    async def test_idle_stop_returns_group_to_stopped_after_last_clip(self) -> None:
        """Nothing left queued must eventually let clients leave the playing state."""
        backend = SendSpinServer()
        backend._stream_lock = asyncio.Lock()
        group = Mock(stop=AsyncMock(), clients=[])
        backend._stream_group = group
        backend._next_play_start_us = 1_000_000
        backend._server = Mock(connected_clients=[])
        play_end_us = 1_000_000
        clock = Mock(now_us=Mock(return_value=play_end_us + 1_000_000))
        backend._schedule_idle_stop(clock, group, play_end_us)
        await backend._idle_stop_task
        group.stop.assert_awaited_once()
        self.assertIsNone(backend._stream_group)

    async def test_idle_stop_is_a_no_op_once_superseded_by_newer_audio(self) -> None:
        """A later play() extending the queue must cancel the pending idle stop."""
        backend = SendSpinServer()
        backend._stream_lock = asyncio.Lock()
        group = Mock(stop=AsyncMock(), clients=[])
        backend._stream_group = group
        play_end_us = 1_000_000
        # A newer clip has already pushed the known end further out by the
        # time this watchdog's wait elapses.
        backend._next_play_start_us = play_end_us + 500_000
        backend._server = Mock(connected_clients=[])
        clock = Mock(now_us=Mock(return_value=play_end_us + 1_000_000))
        backend._schedule_idle_stop(clock, group, play_end_us)
        await backend._idle_stop_task
        group.stop.assert_not_called()
        self.assertIs(backend._stream_group, group)

    async def test_starts_with_installed_api_and_persists_identity(self) -> None:
        """Catch constructor-breaking dependency upgrades and identity rotation."""
        with TemporaryDirectory() as directory:
            state_dir = Path(directory)
            backend = SendSpinServer(advertise=False, state_dir=state_dir)
            with patch(
                "sendspin_service.playback.sendspin.AioSendspinServer.start_server",
                new_callable=AsyncMock,
            ) as start:
                try:
                    await backend._start_server()
                    start.assert_awaited_once()
                    self.assertEqual(backend.connected_client_count(), 0)
                    identity = _load_identity(state_dir)
                    self.assertEqual(identity, _load_identity(state_dir))
                    self.assertEqual(
                        (state_dir / "identity.key").stat().st_mode & 0o777, 0o600
                    )
                finally:
                    await backend._close_server()

    async def test_admits_unpaired_encrypted_player(self) -> None:
        """Connected SDK 5 players must receive playback roles without pairing."""
        server = Mock()
        server.trust_unpaired = AsyncMock()
        server.get_client.return_value.connection_security.psk_category = (
            PskCategory.SENTINEL
        )
        await SendSpinServer._admit_player(server, "browser-player")
        server.trust_unpaired.assert_awaited_once_with("browser-player")

    async def test_preserves_paired_and_legacy_clients(self) -> None:
        """Do not change existing pairing or treat legacy clients as encrypted."""
        for security in (None, Mock(psk_category=PskCategory.LONG_TERM)):
            with self.subTest(security=security):
                server = Mock()
                server.trust_unpaired = AsyncMock()
                server.get_client.return_value.connection_security = security
                await SendSpinServer._admit_player(server, "other-player")
                server.trust_unpaired.assert_not_awaited()

    async def test_rejects_windows_versions_above_supported_maximum(self) -> None:
        """The temporary cap applies regardless of encryption or pairing state."""
        for version in ("2.2.7", "3.0.0", "v3.0.0-beta.1", "10.0.0"):
            with self.subTest(version=version):
                server = Mock(trust_unpaired=AsyncMock())
                client = server.get_client.return_value
                client.info.device_info.product_name = "Sendspin for Windows"
                client.info.device_info.software_version = version
                client.connection.disconnect = AsyncMock()
                with self.assertLogs(
                    "sendspin_service.playback.sendspin", level="WARNING"
                ) as logs:
                    await SendSpinServer._admit_player(server, "windows-player")
                self.assertIn("supported maximum", " ".join(logs.output))
                self.assertIn("2.2.6", " ".join(logs.output))
                client.connection.disconnect.assert_awaited_once_with(
                    retry_connection=False
                )
                server.trust_unpaired.assert_not_awaited()

    async def test_preserves_supported_windows_and_other_clients(self) -> None:
        """Admit Windows 2.2.6 and do not cap unrelated product versions."""
        for product, version in (
            ("Sendspin for Windows", "2.2.6"),
            ("WindowsSpin", "2.2.5"),
            ("Race Voice Browser Player", "3.0.0"),
            ("Other Sendspin client", "10.0.0"),
            ("Sendspin for Windows", None),
            ("Sendspin for Windows", "unknown"),
        ):
            with self.subTest(product=product, version=version):
                server = Mock(trust_unpaired=AsyncMock())
                client = server.get_client.return_value
                client.info.device_info.product_name = product
                client.info.device_info.software_version = version
                client.connection_security.psk_category = PskCategory.SENTINEL
                client.connection.disconnect = AsyncMock()
                await SendSpinServer._admit_player(server, "supported-player")
                client.connection.disconnect.assert_not_awaited()
                server.trust_unpaired.assert_awaited_once_with("supported-player")
