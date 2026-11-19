"""Disk-backed transcript journal. PCM and credentials never enter this database."""
from __future__ import annotations

from dataclasses import replace
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
from typing import Iterator

from .app_log import log_event
from .models import Gap, Segment


# Only missing evidence can be filled; an older journal with no decision code,
# a positive overlap/silence signal, or a speaker conflict remains unchanged.
_GAP_FILL_REASONS = ("no_observations", "missing_left_observation",
                     "missing_right_observation", "observation_gap", "temporary_ocr_absence")


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
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(segments)")}
        if "attribution" not in columns:
            self._db.execute("ALTER TABLE segments ADD COLUMN attribution TEXT NOT NULL DEFAULT 'zoom_source_id'")
            self._db.commit()
        if "attribution_reason" not in columns:
            self._db.execute("ALTER TABLE segments ADD COLUMN attribution_reason TEXT NOT NULL DEFAULT ''")
        # Keep ASR's original row immutable. A late segment, capture gap or manual
        # edit can invalidate a previously inferred name without undoing user edits.
        self._db.executescript("""
            CREATE INDEX IF NOT EXISTS segments_epoch_start ON segments(source_epoch,start);
            CREATE INDEX IF NOT EXISTS segments_epoch_end ON segments(source_epoch,end);
            CREATE TABLE IF NOT EXISTS inferred_gap_fills (
                segment_id TEXT PRIMARY KEY, left_id TEXT NOT NULL, right_id TEXT NOT NULL);
            CREATE VIEW IF NOT EXISTS resolved_segments AS
                SELECT s.id, COALESCE(l.speaker_id, s.speaker_id) AS speaker_id,
                    COALESCE(l.display_name, s.display_name) AS display_name,
                    s.start, s.end, s.text, s.status, s.source_epoch,
                    CASE WHEN l.id IS NULL THEN s.attribution ELSE 'inferred_gap_fill' END AS attribution,
                    s.attribution_reason
                FROM segments AS s LEFT JOIN inferred_gap_fills AS f ON f.segment_id=s.id
                    LEFT JOIN segments AS l ON l.id=f.left_id;
        """)
        self.set_metadata("schema_version", 3)

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
            self._db.execute("BEGIN IMMEDIATE")  # Serialize the read/validate/write across connections too.
            old = self._db.execute("SELECT * FROM segments WHERE id=?", (segment.id,)).fetchone()
            if old:
                previous = self._segment(old)
                if previous.attribution == "manual":
                    segment = replace(segment, display_name=previous.display_name, attribution="manual",
                                      attribution_reason=previous.attribution_reason)
                if previous == segment:
                    return  # Idempotent delivery/replay.
                if previous.status == "final":
                    raise ValueError("Финальную реплику нельзя перезаписать.")
                if (previous.speaker_id, previous.source_epoch, previous.start) != (segment.speaker_id, segment.source_epoch, segment.start):
                    raise ValueError("Источник и начало реплики неизменяемы.")
            self._db.execute("""INSERT INTO segments
                (id,speaker_id,display_name,start,end,text,status,source_epoch,attribution,attribution_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name, end=excluded.end,
                text=excluded.text,status=excluded.status,attribution=excluded.attribution,
                attribution_reason=excluded.attribution_reason""",
                (segment.id, segment.speaker_id, segment.display_name, segment.start, segment.end,
                 segment.text, segment.status, segment.source_epoch, segment.attribution, segment.attribution_reason))
            changes = self._refresh_gap_fills(segment.source_epoch, segment.start, max(segment.end, old["end"] if old else 0))
        self._log_fill_changes(changes)

    @staticmethod
    def _log_fill_changes(changes):
        # Only called after commit, including when the right anchor arrives late.
        for event, segment_id, left, right in changes:
            log_event(event, segment_id=segment_id, left_anchor_id=left, right_anchor_id=right)

    def _refresh_gap_fills(self, epoch: str, start: float, end: float) -> list:
        """Re-evaluate short unknowns touched by this edit inside the same write transaction."""
        # An affected unknown starts at most 2s (anchor gap) + 1.5s (duration)
        # before the edit, or less than 2s after it. Only a bounded time range
        # of the index is scanned for ordinary inserts. Include newly corrected
        # candidates so their old inference is withdrawn in the same transaction.
        candidates = self._db.execute("""SELECT s.id,s.start,s.end,s.status,s.speaker_id,
                s.attribution,s.attribution_reason,f.left_id,f.right_id
            FROM segments s LEFT JOIN inferred_gap_fills f ON f.segment_id=s.id
            WHERE s.source_epoch=? AND s.start BETWEEN ? AND ? AND round(s.end-s.start,9)<1.5
              AND (f.segment_id IS NOT NULL OR (s.status='final' AND s.speaker_id='local:remote'
              AND s.attribution='unattributed_mix' AND s.attribution_reason IN (?,?,?,?,?)))""",
            (epoch, start - 3.5, end + 2, *_GAP_FILL_REASONS)).fetchall()
        changes = []
        for candidate in candidates:
            old = (candidate["left_id"], candidate["right_id"]) if candidate["left_id"] else None
            new = self._gap_anchors(epoch, candidate) if candidate["attribution"] == "unattributed_mix" else None
            if old == new:
                continue
            if old:
                self._db.execute("DELETE FROM inferred_gap_fills WHERE segment_id=?", (candidate["id"],))
                changes.append(("gap_fill_withdrawn", candidate["id"], *old))
            if new:
                self._db.execute("INSERT INTO inferred_gap_fills VALUES (?,?,?)", (candidate["id"], *new))
                changes.append(("gap_fill_applied", candidate["id"], *new))
        return changes

    def _gap_anchors(self, epoch, candidate):
        # Always choose adjacent raw rows (including mic, manual, preliminary,
        # and unknown rows). Never skip a non-anchor or chain through an inference.
        cols = "id, speaker_id, display_name, attribution, status, start, end"
        left = self._db.execute(f"""SELECT {cols} FROM segments WHERE source_epoch=?
            AND end<=? AND end>? ORDER BY end DESC LIMIT 1""",
            (epoch, candidate["start"], candidate["start"] - 2)).fetchone()
        right = self._db.execute(f"""SELECT {cols} FROM segments WHERE source_epoch=?
            AND start>=? AND start<? ORDER BY start LIMIT 1""",
            (epoch, candidate["end"], candidate["end"] + 2)).fetchone()
        if not all(row and row["status"] == "final" and row["attribution"] == "zoom_ocr_hint"
                   and row["speaker_id"].startswith("local:zoom-ocr:")
                   and row["speaker_id"].split(":", 2)[-1] not in {"", "unknown"}
                   and row["display_name"].strip() for row in (left, right)):
            return None
        if (left["speaker_id"], left["display_name"]) != (right["speaker_id"], right["display_name"]):
            return None
        if max(round(candidate["start"] - left["end"], 9), round(right["start"] - candidate["end"], 9)) >= 2:
            return None
        # Covers overlaps AND ties, including another speaker ending when the
        # left anchor ends. A missing audio interval also breaks the bracket.
        conflict = self._db.execute("""SELECT 1 FROM segments WHERE source_epoch=?
            AND start<=? AND end>=? AND id NOT IN (?,?,?) LIMIT 1""",
            (epoch, right["start"], left["end"], left["id"], candidate["id"], right["id"])).fetchone()
        if conflict or self._db.execute("""SELECT 1 FROM gaps WHERE source_epoch=? AND start<=? AND end>=?
                LIMIT 1""", (epoch, right["start"], left["end"])).fetchone():
            return None
        return left["id"], right["id"]

    append = upsert

    def correct_speaker(self, segment_id: str, name: str) -> None:
        """Explicit user correction; preserve text and timestamp, retain audit event."""
        name = name.strip()
        if not name or len(name) > 150 or any(ord(c) < 32 for c in name):
            raise ValueError("Введите имя длиной от 1 до 150 символов.")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT speaker_id,display_name,source_epoch,start,end FROM segments WHERE id=?", (segment_id,)).fetchone()
            if not row:
                raise ValueError("Реплика не найдена.")
            self._db.execute("UPDATE segments SET display_name=?,attribution='manual',attribution_reason='manual_correction' WHERE id=?", (name, segment_id))
            self._db.execute("INSERT INTO participant_events(user_id,display_name,event,source_epoch) VALUES (?,?,?,?)",
                             (row[0], name, "manual_correction:" + segment_id, row[2]))
            changes = self._refresh_gap_fills(row["source_epoch"], row["start"], row["end"])
        self._log_fill_changes(changes)

    @staticmethod
    def _segment(row) -> Segment:
        return Segment(**{name: row[name] for name in Segment.__dataclass_fields__})

    def iter_segments(self) -> Iterator[Segment]:
        # A separate read connection gives an export a consistent snapshot while
        # the worker continues writing. fetchmany bounds Python memory.
        if str(self.path) == ":memory:":
            with self._lock:
                cursor = self._db.execute("SELECT * FROM resolved_segments ORDER BY start,id")
                while rows := cursor.fetchmany(256):
                    yield from (self._segment(row) for row in rows)
            return
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute("SELECT * FROM resolved_segments ORDER BY start,id")
            while rows := cursor.fetchmany(256):
                yield from (self._segment(row) for row in rows)
        finally:
            connection.close()

    def segment_by_id(self, segment_id: str) -> Segment | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM resolved_segments WHERE id=?", (segment_id,)).fetchone()
            return self._segment(row) if row is not None else None

    def recent(self, limit: int = 500) -> list[Segment]:
        limit = max(0, min(int(limit), 2000))
        with self._lock:
            rows = self._db.execute("SELECT * FROM resolved_segments ORDER BY start DESC,id DESC LIMIT ?", (limit,)).fetchall()
        return [self._segment(row) for row in reversed(rows)]

    def gap(self, speaker_id: str, start: float, end: float, reason: str, source_epoch: str = "0") -> None:
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end < start:
            raise ValueError("Некорректный интервал пропуска.")
        with self._lock, self._db:
            self._db.execute("INSERT INTO gaps(speaker_id,start,end,reason,source_epoch) VALUES (?,?,?,?,?)",
                             (speaker_id, start, end, reason[:128], source_epoch))
            changes = self._refresh_gap_fills(source_epoch, start, end)
        self._log_fill_changes(changes)

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

    def iter_participants(self):
        """Observed speaker/name snapshots, grouped by actual source identity."""
        with self._lock:
            cursor = self._db.execute("""SELECT speaker_id,source_epoch,display_name,
                MIN(start) AS first_speech,MAX(end) AS last_speech FROM resolved_segments
                GROUP BY speaker_id,source_epoch,display_name ORDER BY first_speech""")
        while True:
            with self._lock:
                rows = cursor.fetchmany(256)
            if not rows:
                break
            yield from (dict(row) for row in rows)

    def iter_participant_events(self):
        with self._lock:
            cursor = self._db.execute("SELECT user_id,display_name,event,source_epoch,timestamp_ms FROM participant_events ORDER BY id")
        while True:
            with self._lock:
                rows = cursor.fetchmany(256)
            if not rows:
                break
            yield from (dict(row) for row in rows)

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
