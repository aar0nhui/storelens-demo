"""SQLite event store. Only events and aggregates are persisted: NO frames, NO trajectories, NO faces.

visits       one row per anonymous visit (rotating-salt hash id, role, first/last seen)
zone_visits  per-visit per-zone exposure facts (dwell, stopped, facing, engaged)
entries      door crossings
heat         floor-grid occupancy seconds per time bucket (aggregate)
audience     OPTIONAL demographics: counts per (time bucket, display, metric, age band, perceived gender). Aggregate
             only: there is deliberately NO visit key or timestamp finer than the bucket, so a row cannot be joined
             back to a visit, a zone visit or a path.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, camera_id TEXT, source TEXT,
  t_start REAL, t_end REAL, created_at TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS visits (
  run_id INTEGER, visit_key TEXT, role TEXT, t_first REAL, t_last REAL, duration_s REAL,
  PRIMARY KEY (run_id, visit_key));
CREATE TABLE IF NOT EXISTS zone_visits (
  run_id INTEGER, visit_key TEXT, zone_id TEXT, t_enter REAL, t_exit REAL, dwell_s REAL,
  stopped_s REAL, facing_stopped_s REAL);
CREATE TABLE IF NOT EXISTS entries (run_id INTEGER, visit_key TEXT, t REAL, direction INTEGER);
CREATE TABLE IF NOT EXISTS heat (
  run_id INTEGER, bucket_ts REAL, gx INTEGER, gy INTEGER, seconds REAL,
  PRIMARY KEY (run_id, bucket_ts, gx, gy));
CREATE TABLE IF NOT EXISTS audience (
  run_id INTEGER, bucket_ts REAL, zone_id TEXT, metric TEXT, age_band TEXT, gender TEXT, n INTEGER,
  PRIMARY KEY (run_id, bucket_ts, zone_id, metric, age_band, gender));
CREATE INDEX IF NOT EXISTS ix_zv ON zone_visits(run_id, zone_id);
CREATE INDEX IF NOT EXISTS ix_visits ON visits(run_id, role);
"""


class EventStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)

    def begin_run(self, label: str, camera_id: str, source: str, t_start: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(label, camera_id, source, t_start, created_at, meta) VALUES (?,?,?,?,?,?)",
            (label, camera_id, Path(source).name, t_start, time.strftime("%Y-%m-%dT%H:%M:%S"), "{}"))
        self.conn.commit()
        return int(cur.lastrowid)

    def add_visit(self, run_id: int, key: str, role: str, t_first: float, t_last: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO visits VALUES (?,?,?,?,?,?)",
                          (run_id, key, role, t_first, t_last, t_last - t_first))

    def add_zone_visits(self, run_id: int, key: str, zvs) -> None:
        self.conn.executemany(
            "INSERT INTO zone_visits VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, key, z.zone_id, z.t_enter, z.t_exit, z.dwell_s, z.stopped_s, z.facing_stopped_s)
             for z in zvs])

    def add_entries(self, run_id: int, key: str, entries) -> None:
        self.conn.executemany("INSERT INTO entries VALUES (?,?,?,?)", [(run_id, key, t, d) for t, d in entries])

    def add_heat(self, run_id: int, heat: dict) -> None:
        self.conn.executemany(
            "INSERT INTO heat VALUES (?,?,?,?,?) ON CONFLICT(run_id, bucket_ts, gx, gy) "
            "DO UPDATE SET seconds = seconds + excluded.seconds",
            [(run_id, b, gx, gy, s) for (b, gx, gy), s in heat.items()])

    def add_audience(self, run_id: int, counts: dict) -> None:
        self.conn.executemany(
            "INSERT INTO audience VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id, bucket_ts, zone_id, metric, age_band, gender) "
            "DO UPDATE SET n = n + excluded.n",
            [(run_id, b, zone, metric, age, gender, n) for (b, zone, metric, age, gender), n in counts.items()])

    def finish_run(self, run_id: int, t_end: float, meta: dict) -> None:
        self.conn.execute("UPDATE runs SET t_end=?, meta=? WHERE run_id=?", (t_end, json.dumps(meta), run_id))
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def query(self, sql: str, args: tuple = ()) -> list[tuple]:
        return self.conn.execute(sql, args).fetchall()
