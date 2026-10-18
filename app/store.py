"""SQLite storage for feed definitions (no web-framework dependency)."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any


class FeedNotFound(LookupError):
    pass


class FeedStore:
    def __init__(self, db_path: str, default_feed: tuple[str, str, str, str] | None = None):
        """`default_feed` = (name, url, markup_percent, markup_fixed); inserted exactly once into a fresh DB."""
        self.db_path = db_path
        self.default_feed = default_feed
        self._ready = False
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS feeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL,
                    markup_percent TEXT NOT NULL DEFAULT '0', markup_fixed TEXT NOT NULL DEFAULT '0',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
                conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                # The default feed is seeded once. Re-seeding on "table is empty" made it impossible
                # to delete the last feed, and duplicated feeds in databases created by older versions.
                first_run = conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('seeded', '1')").rowcount == 1
                has_feeds = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] > 0
                if first_run and not has_feeds and self.default_feed and self.default_feed[1]:
                    conn.execute("INSERT INTO feeds(name, url, markup_percent, markup_fixed) VALUES(?,?,?,?)", self.default_feed)
                conn.commit()
            self._ready = True

    def list(self) -> list[dict[str, Any]]:
        self._ensure_ready()
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM feeds ORDER BY id")]

    def get(self, feed_id: int | None = None) -> dict[str, Any]:
        """Feed by id, or the first feed when `feed_id` is None."""
        self._ensure_ready()
        with closing(self._connect()) as conn:
            if feed_id is None:
                row = conn.execute("SELECT * FROM feeds ORDER BY id LIMIT 1").fetchone()
            else:
                row = conn.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
        if row is None:
            raise FeedNotFound("Feed not found. Add a feed in the dashboard first.")
        return dict(row)

    def create(self, name: str, url: str, markup_percent: str, markup_fixed: str) -> dict[str, Any]:
        self._ensure_ready()
        with closing(self._connect()) as conn:
            cur = conn.execute("INSERT INTO feeds(name, url, markup_percent, markup_fixed) VALUES(?,?,?,?)",
                               (name, url, markup_percent, markup_fixed))
            conn.commit()
            return dict(conn.execute("SELECT * FROM feeds WHERE id=?", (cur.lastrowid,)).fetchone())

    def delete(self, feed_id: int) -> None:
        self._ensure_ready()
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
            conn.commit()
            if not cur.rowcount:
                raise FeedNotFound("Feed not found")
