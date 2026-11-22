"""Cache file locks and active scrape job guards."""

from __future__ import annotations

import threading
from pathlib import Path

_CACHE_LOCKS: dict[str, threading.RLock] = {}
_REGISTRY_GUARD = threading.Lock()


def cache_lock_for(path: Path) -> threading.RLock:
    """One re-entrant lock per cache file path (shared by every cache store instance)."""
    key = str(Path(path).resolve())
    with _REGISTRY_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.RLock())


class JobGuard:
    """Tracks live scrape jobs so they cannot race on the same queries.

    * One active live job per user.
    * No two active jobs may claim the same normalized query.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_user: dict[str, str] = {}
        self._query_owner: dict[str, str] = {}  # query_key -> job_id

    def try_acquire(self, user_id: str, job_id: str, query_keys: set[str]) -> str | None:
        """Return None on success, or a human-readable conflict reason."""
        with self._lock:
            if user_id in self._by_user:
                return f"You already have an active scrape job ({self._by_user[user_id]})"
            clash = sorted(q for q in query_keys if q in self._query_owner)
            if clash:
                return f"Another active job is already scraping: {', '.join(clash[:3])}"
            self._by_user[user_id] = job_id
            for q in query_keys:
                self._query_owner[q] = job_id
            return None

    def release(self, user_id: str, job_id: str) -> None:
        with self._lock:
            if self._by_user.get(user_id) == job_id:
                del self._by_user[user_id]
            for q in [q for q, j in self._query_owner.items() if j == job_id]:
                del self._query_owner[q]

    def active_job_ids(self) -> list[str]:
        with self._lock:
            return list(self._by_user.values())
