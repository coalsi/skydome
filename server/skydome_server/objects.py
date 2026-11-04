"""The sky watcher's log for the web page (DECISIONS.md D-030): what it saw,
what it made of it, and the labels you give it, which it learns from.

tools/run_watch.py writes var/watch/objects.db (skydome_pipeline/skywatch.py
has the table); this reads it and stores labels, bumping meta "labels" so
the watcher relearns within seconds.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from skydome_pipeline.skywatch import LABELS, open_log

COLUMNS = ("id", "start", "end", "frames", "kind", "auto_kind", "confidence", "source", "label", "match", "match_id",
           "detail", "shower", "unknown", "alerted", "sun_alt", "path", "thumb", "clip")


@dataclass
class ObjectLog:
    db_path: Path
    media: Path

    def _db(self) -> sqlite3.Connection:
        return open_log(self.db_path)

    def _row(self, r: tuple) -> dict:
        o = dict(zip(COLUMNS, r))
        o["detail"] = json.loads(o["detail"]) if o["detail"] else None
        o["path"] = json.loads(o["path"]) if o["path"] else []
        o["unknown"], o["alerted"] = bool(o["unknown"]), bool(o["alerted"])
        for k in ("thumb", "clip"):
            o[k] = f"/api/objects/media/{o[k]}" if o[k] else None
        return o

    def list(self, limit: int = 100, before: int | None = None, kind: str | None = None,
             unknown: bool = False, since: float | None = None) -> list[dict]:
        where, args = [], []
        if before:
            where.append("id < ?")
            args.append(before)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if unknown:
            where.append("unknown = 1")
        if since:
            where.append("start >= ?")
            args.append(since)
        sql = f"SELECT {', '.join(COLUMNS)} FROM objects" + (f" WHERE {' AND '.join(where)}" if where else "") \
            + " ORDER BY id DESC LIMIT ?"
        with self._db() as db:
            return [self._row(r) for r in db.execute(sql, (*args, max(1, min(limit, 500)))).fetchall()]

    def get(self, oid: int) -> dict | None:
        with self._db() as db:
            r = db.execute(f"SELECT {', '.join(COLUMNS)} FROM objects WHERE id = ?", (oid,)).fetchone()
        return self._row(r) if r else None

    def set_label(self, oid: int, label: str | None) -> dict | None:
        """Your label (None takes it back): it becomes what the object is, and an example to learn from."""
        if label is not None:
            label = label.strip().lower()[:40]
            if not label or not all(c.isalnum() or c in " -_" for c in label):
                raise ValueError("a label is a word or two: letters, numbers, spaces, - and _")
        with self._db() as db:
            if not db.execute("SELECT 1 FROM objects WHERE id = ?", (oid,)).fetchone():
                return None
            db.execute("UPDATE objects SET label = ?, kind = COALESCE(?, auto_kind), "
                       "unknown = (COALESCE(?, auto_kind) = 'unknown') WHERE id = ?", (label, label, label, oid))
            db.execute("INSERT INTO meta (key, value) VALUES ('labels', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (f"{time.time():.3f}",))
        return self.get(oid)

    def labels(self) -> list[str]:
        """The usual kinds, then any of your own."""
        with self._db() as db:
            own = [r[0] for r in db.execute("SELECT DISTINCT label FROM objects WHERE label IS NOT NULL")]
        return list(LABELS) + sorted(set(own) - set(LABELS))

    def summary(self, hours: float = 24) -> dict:
        since = time.time() - hours * 3600
        with self._db() as db:
            counts = dict(db.execute("SELECT kind, COUNT(*) FROM objects WHERE start >= ? GROUP BY kind", (since,)))
            unknown = db.execute("SELECT COUNT(*) FROM objects WHERE start >= ? AND unknown = 1", (since,)).fetchone()[0]
            learned = db.execute("SELECT COUNT(*) FROM objects WHERE label IS NOT NULL").fetchone()[0]
        return {"hours": hours, "counts": counts, "unknown": unknown, "labelled": learned}

    def media_file(self, rel: str) -> Path | None:
        p = (self.media / rel).resolve()
        if self.media.resolve() not in p.parents or not p.is_file():
            return None
        return p
