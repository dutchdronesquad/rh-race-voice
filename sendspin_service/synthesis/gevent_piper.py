"""PiperSynthesizer variant for use inside RH's gevent-patched process."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gevent import get_hub

from .piper import PiperSynthesizer

if TYPE_CHECKING:
    from collections.abc import Callable


class GeventPiperSynthesizer(PiperSynthesizer):
    """Offload blocking Piper/ONNX work to gevent's native threadpool."""

    def _run_native[T](
        self, function: Callable[..., T], *args: Any, **kwargs: Any
    ) -> T:
        """Isolate inference from the gevent event loop via the hub's native pool.

        RotorHazard monkey-patches threading, so its ordinary executor does not
        provide that isolation. Hold a cooperative lock on the calling side to
        serialize native work.
        """
        with self._native_lock:
            return get_hub().threadpool.apply(function, args, kwargs)
