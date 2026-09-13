"""Cover the aiosendspin 9 server API and open playback admission."""

# Use the standard-library test runner.
# ruff: noqa: PT009, SLF001

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from aiosendspin.noise.trust_store import PskCategory

from sendspin_service.sendspin import SendSpinServer, _load_identity


class SendspinStartupTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real dependency constructor without opening a network port."""

    async def test_starts_with_installed_api_and_persists_identity(self) -> None:
        """Catch constructor-breaking dependency upgrades and identity rotation."""
        with TemporaryDirectory() as directory:
            state_dir = Path(directory)
            backend = SendSpinServer(advertise=False, state_dir=state_dir)
            with patch(
                "sendspin_service.sendspin.AioSendspinServer.start_server",
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
