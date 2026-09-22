"""Persistent status-history boundary for the desktop application."""

from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class Observation:
    id: int
    timestamp: str
    mode: int
    motion_status: int | None = None
    selected_gripper: int | None = None
    ptp_speed_pct: float | None = None
    linear_speed_mm_s: float | None = None
    speed_ratio_pct: float | None = None
    acceleration_ms: float | None = None
    deceleration_ms: float | None = None
    commanded_joints: str | None = None
    encoder_joints: str | None = None
    commanded_position: str | None = None


@dataclass(frozen=True)
class HistoryPage:
    rows: tuple[Observation, ...]
    total: int


@dataclass(frozen=True)
class HistoryValue:
    timestamp: str
    field: str
    value: str


@dataclass(frozen=True)
class HistoryValuePage:
    rows: tuple[HistoryValue, ...]
    total: int


class ObservationStore:
    EXTRA_COLUMNS = {
        "motion_status": "INTEGER", "selected_gripper": "INTEGER",
        "ptp_speed_pct": "REAL", "linear_speed_mm_s": "REAL", "speed_ratio_pct": "REAL",
        "acceleration_ms": "REAL", "deceleration_ms": "REAL",
        "commanded_joints": "TEXT", "encoder_joints": "TEXT", "commanded_position": "TEXT",
    }
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS observations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, mode INTEGER NOT NULL CHECK(mode BETWEEN 0 AND 3))"
        )
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(observations)")}
        for name, sql_type in self.EXTRA_COLUMNS.items():
            if name not in existing:
                self.connection.execute(f"ALTER TABLE observations ADD COLUMN {name} {sql_type}")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_observations_timestamp ON observations(timestamp)"
        )
        self.connection.commit()

    def __enter__(self) -> "ObservationStore": return self
    def __exit__(self, *_: object) -> None: self.close()
    def close(self) -> None: self.connection.close()

    def append_many(self, observations: Iterable[tuple | dict]) -> None:
        rows = []
        for item in observations:
            if isinstance(item, dict):
                rows.append(tuple(item.get(name) for name in self.column_names()))
            else:
                values = tuple(item)
                rows.append(values + (None,) * (len(self.column_names()) - len(values)))
        columns = ",".join(self.column_names())
        marks = ",".join("?" for _ in self.column_names())
        self.connection.executemany(f"INSERT INTO observations({columns}) VALUES ({marks})", rows)
        self.connection.commit()

    @classmethod
    def column_names(cls) -> tuple[str, ...]:
        return ("timestamp", "mode", *cls.EXTRA_COLUMNS)

    def available_hours(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT DISTINCT substr(timestamp, 1, 13) AS hour "
            "FROM observations ORDER BY hour"
        ).fetchall()
        return tuple(row[0] for row in rows)

    def query(self, *, limit: int, offset: int = 0, start: str = "", end: str = "",
              modes: set[int] | None = None) -> HistoryPage:
        conditions, params = [], []
        if start: conditions.append("timestamp >= ?"); params.append(start)
        if end: conditions.append("timestamp <= ?"); params.append(end)
        if modes:
            marks = ",".join("?" for _ in modes)
            conditions.append(f"mode IN ({marks})"); params.extend(sorted(modes))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        total = self.connection.execute(f"SELECT COUNT(*) FROM observations{where}", params).fetchone()[0]
        select_columns = "id," + ",".join(self.column_names())
        rows = self.connection.execute(
            f"SELECT {select_columns} FROM observations{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return HistoryPage(tuple(Observation(*row) for row in rows), total)

    def query_values(self, *, fields: tuple[str, ...], limit: int, offset: int = 0,
                     start: str = "", end: str = "") -> HistoryValuePage:
        """Return the wide snapshots as a paginated time/field/value history view."""
        allowed = set(self.column_names()) - {"timestamp"}
        if not fields or any(field not in allowed for field in fields):
            raise ValueError("History field is not available")
        selects, params = [], []
        for field in fields:
            conditions = [f"{field} IS NOT NULL"]
            branch_params: list[str] = []
            if start: conditions.append("timestamp >= ?"); branch_params.append(start)
            if end: conditions.append("timestamp <= ?"); branch_params.append(end)
            selects.append(
                f"SELECT timestamp, '{field}' AS field, CAST({field} AS TEXT) AS value "
                f"FROM observations WHERE {' AND '.join(conditions)}"
            )
            params.extend(branch_params)
        union = " UNION ALL ".join(selects)
        total = self.connection.execute(f"SELECT COUNT(*) FROM ({union})", params).fetchone()[0]
        rows = self.connection.execute(
            f"SELECT timestamp,field,value FROM ({union}) "
            "ORDER BY timestamp DESC, field LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return HistoryValuePage(tuple(HistoryValue(*row) for row in rows), total)

    @staticmethod
    def encode_values(values: tuple[float, ...]) -> str:
        return json.dumps([round(value, 6) for value in values], separators=(",", ":"))

    def interval_frame(self, start: str, end: str) -> pd.DataFrame:
        previous = self.connection.execute(
            "SELECT timestamp,mode FROM observations WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1", (start,)
        ).fetchall()
        rows = previous + self.connection.execute(
            "SELECT timestamp,mode FROM observations WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp", (start, end)
        ).fetchall()
        if not rows:
            return pd.DataFrame(columns=["timestamp", "mode"])
        return pd.DataFrame(rows, columns=["timestamp", "mode"]).sort_values("timestamp")

    def diagnostic_interval_frame(self, start: str, end: str) -> pd.DataFrame:
        """Return the last state before a recording plus all rich snapshots in it."""
        columns = self.column_names()
        selected = ",".join(columns)
        previous = self.connection.execute(
            f"SELECT {selected} FROM observations WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1", (start,)
        ).fetchall()
        rows = previous + self.connection.execute(
            f"SELECT {selected} FROM observations WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp", (start, end)
        ).fetchall()
        return pd.DataFrame(rows, columns=columns).sort_values("timestamp") if rows else pd.DataFrame(columns=columns)
