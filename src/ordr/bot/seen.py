"""The bot's own record of message IDs it has already handled (TRD section 4, step 2).

A separate SQLite file from the gate's database. Meta retries a webhook for up to 7 days, so rows
are kept 8 days.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

KEEP = timedelta(days=8)


class SeenStore:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_messages "
            "(message_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)"
        )
        self.prune()

    def claim(self, message_id: str) -> bool:
        """True the first time an ID is seen, False for every repeat. One atomic insert."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO seen_messages (message_id, seen_at) VALUES (?, ?)",
            (message_id, datetime.now(UTC).isoformat()),
        )
        return cursor.rowcount == 1

    def prune(self) -> None:
        cutoff = (datetime.now(UTC) - KEEP).isoformat()
        self._conn.execute("DELETE FROM seen_messages WHERE seen_at < ?", (cutoff,))

    def close(self) -> None:
        self._conn.close()
