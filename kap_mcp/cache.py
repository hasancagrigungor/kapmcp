"""Small thread-safe TTL cache shared by the market and document layers.

Kept dependency-free on purpose; a Redis-backed implementation can replace it
behind the same three methods when the server runs multi-process.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional


class TTLCache:
    def __init__(self, max_entries: int = 2048) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._max = max_entries

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            if hit:
                del self._data[key]
        return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            if len(self._data) >= self._max:
                # Drop the oldest-expiring third; cheap and good enough.
                for k, _ in sorted(self._data.items(), key=lambda kv: kv[1][0])[: self._max // 3]:
                    del self._data[k]
            self._data[key] = (time.monotonic() + ttl, value)

    def get_or_set(self, key: str, ttl: float, loader: Callable[[], Any]) -> Any:
        hit = self.get(key)
        if hit is not None:
            return hit
        value = loader()
        self.set(key, value, ttl)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
