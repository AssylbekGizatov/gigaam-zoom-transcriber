"""Disk-backed transcript journal. PCM and credentials never enter this database."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sqlite3
import threading
from typing import Iterator

from .models import Gap, Segment


class Store:
    def __init__(self, path: str | Path):
        if str(path).startswith(("\\\\", "//")):
            raise ValueError("Выберите локальную папку для журнала встречи.")
        self.path = Path(path).expanduser().resolve() if str(path) != ":memory:" else Path(":memory:")
        if str(self.path).startswith(("\\\\", "//")):
            raise ValueError("Выберите локальную папку для журнала встречи.")
        if os.name == "nt" and str(path) != ":memory:":
            import ctypes
            drive_type = ctypes.windll.kernel32.GetDriveTypeW
            drive_type.argtypes = [ctypes.c_wchar_p]
            drive_type.restype = ctypes.c_uint
            if drive_type(self.path.anchor) in {0, 1, 4}:
                raise ValueError("Для журнала встречи нужен доступный локальный диск.")
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA cache_size=-2048;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS segments (
                id TEXT PRIMARY KEY, speaker_id TEXT NOT NULL, display_name TEXT NOT NULL,
                start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('preliminary','final')),
                source_epoch TEXT NOT NULL,
                CHECK(start >= 0 AND end > start));
            CREATE INDEX IF NOT EXISTS segments_time ON segments(start, id);
            CREATE TABLE IF NOT EXISTS gaps (
                id INTEGER PRIMARY KEY, speaker_id TEXT NOT NULL, start REAL NOT NULL,
                end REAL NOT NULL, reason TEXT NOT NULL, source_epoch TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS participant_events (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, display_name TEXT,
                event TEXT NOT NULL, source_epoch TEXT NOT NULL, timestamp_ms INTEGER);
            CREATE INDEX IF NOT EXISTS participants_source
                ON participant_events(source_epoch,user_id,id);
            CREATE TABLE IF NOT EXISTS source_cursors (
                user_id TEXT NOT NULL, source_epoch TEXT NOT NULL, end REAL NOT NULL,
                PRIMARY KEY(user_id,source_epoch));
        """)
        self.set_metadata("schema_version", 1)

    def set_metadata(self, key: str, value) -> None:
        # Only structural metadata is permitted; secrets belong in OS keyring.
        if any(word in key.lower() for word in ("token", "secret", "password", "authorization", "credential")):
            raise ValueError("Секреты нельзя сохранять в журнал встречи.")
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(key) > 128 or len(encoded) > 65536:
            raise ValueError("Слишком большой элемент метаданных.")
        with self._lock, self._db:
            self._db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encoded))

    def get_metadata(self, key: str | None = None, default=None):
        with self._lock:
            if key is None:
                return {row["key"]: json.loads(row["value"]) for row in self._db.execute("SELECT key,value FROM metadata")}
            row = self._db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def upsert(self, segment: Segment) -> None:
        if (not all(math.isfinite(v) for v in (segment.start, segment.end))
                or segment.start < 0 or segment.end <= segment.start
                or segment.status not in {"preliminary", "final"}
                or not segment.id or not isinstance(segment.text, str)
                or len(segment.text) > 100000):
            raise ValueError("Некорректная реплика.")
        with self._lock, self._db:
            old = self._db.execute("SELECT * FROM segments WHERE id=?", (segment.id,)).fetchone()
            if old:
                previous = self._segment(old)
                if previous == segment:
                    return  # Idempotent delivery/replay.
                if previous.status == "final":
                    raise ValueError("Финальную реплику нельзя перезаписать.")
                if (previous.speaker_id, previous.source_epoch, previous.start) != (segment.speaker_id, segment.source_epoch, segment.start):
                    raise ValueError("Источник и начало реплики неизменяемы.")
            self._db.execute("""INSERT INTO segments VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,
                end=excluded.end,text=excluded.text,status=excluded.status""",
                (segment.id, segment.speaker_id, segment.display_name, segment.start,
                 segment.end, segment.text, segment.status, segment.source_epoch))

    append = upsert

    @staticmethod
    def _segment(row) -> Segment:
        return Segment(**{name: row[name] for name in Segment.__dataclass_fields__})

    def iter_segments(self) -> Iterator[Segment]:
        # A separate read connection gives an export a consistent snapshot while
        # the worker continues writing. fetchmany bounds Python memory.
        if str(self.path) == ":memory:":
            with self._lock:
                cursor = self._db.execute("SELECT * FROM segments ORDER BY start,id")
                while rows := cursor.fetchmany(256):
                    yield from (self._segment(row) for row in rows)
            return
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute("SELECT * FROM segments ORDER BY start,id")
            while rows := cursor.fetchmany(256):
                yield from (self._segment(row) for row in rows)
        finally:
            connection.close()

    def recent(self, limit: int = 500) -> list[Segment]:
        limit = max(0, min(int(limit), 2000))
        with self._lock:
            rows = self._db.execute("SELECT * FROM segments ORDER BY start DESC,id DESC LIMIT ?", (limit,)).fetchall()
        return [self._segment(row) for row in reversed(rows)]

    def gap(self, speaker_id: str, start: float, end: float, reason: str, source_epoch: str = "0") -> None:
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end < start:
            raise ValueError("Некорректный интервал пропуска.")
        with self._lock, self._db:
            self._db.execute("INSERT INTO gaps(speaker_id,start,end,reason,source_epoch) VALUES (?,?,?,?,?)",
                             (speaker_id, start, end, reason[:128], source_epoch))

    def iter_gaps(self) -> Iterator[Gap]:
        # Fetch a bounded batch at a time; gaps are audit information, not audio.
        with self._lock:
            cursor = self._db.execute("SELECT speaker_id,start,end,reason,source_epoch FROM gaps ORDER BY start,id")
        while True:
            with self._lock:
                rows = cursor.fetchmany(256)
            if not rows:
                break
            yield from (Gap(**dict(row)) for row in rows)

    def participant_event(self, user_id: str, name: str | None, event: str,
                          epoch: str = "0", timestamp_ms: int | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO participant_events(user_id,display_name,event,source_epoch,timestamp_ms) VALUES (?,?,?,?,?)",
                             (user_id, name, event, epoch, timestamp_ms))

    def participant_name(self, user_id: str, epoch: str = "0") -> str | None:
        with self._lock:
            row = self._db.execute("""SELECT display_name FROM participant_events
                WHERE user_id=? AND source_epoch=? AND display_name IS NOT NULL
                ORDER BY id DESC LIMIT 1""", (user_id, epoch)).fetchone()
        return row[0] if row else None

    def set_source_cursor(self, user_id: str, epoch: str, end: float) -> None:
        with self._lock, self._db:
            self._db.execute("""INSERT INTO source_cursors VALUES (?,?,?)
                ON CONFLICT(user_id,source_epoch) DO UPDATE SET end=MAX(end,excluded.end)""", (user_id, epoch, end))

    def source_cursor(self, user_id: str, epoch: str) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT end FROM source_cursors WHERE user_id=? AND source_epoch=?", (user_id, epoch)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._db.close()
                self._db = None
