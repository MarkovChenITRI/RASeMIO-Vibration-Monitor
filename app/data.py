"""CSV readers and session persistence."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np


def infer_sensor_start(path: Path, metadata: dict[str, str]) -> datetime | None:
    """Read the local recording start from metadata, falling back to RecData filename."""
    date_value = next((value for key, value in metadata.items() if "date" in key.lower()), "")
    if date_value:
        parsed = pd.to_datetime(date_value).to_pydatetime()
        return parsed.astimezone() if parsed.tzinfo is None else parsed
    match = re.search(r"RecData--(\d{14})(?:\D|$)", path.name, flags=re.IGNORECASE)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").astimezone()
    return None


def load_sensor_csv(path: Path) -> pd.DataFrame:
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        preview = [stream.readline() for _ in range(40)]
    metadata: dict[str, str] = {}
    try:
        header = next(i for i, line in enumerate(preview) if "time" in line.lower() and "," in line)
    except StopIteration as exc:
        raise ValueError("找不到 Sensor CSV 的 Time,X,Y,Z 欄位") from exc
    for line in preview[:header]:
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    frame = pd.read_csv(path, skiprows=header)
    frame.columns = frame.columns.str.strip()
    aliases = {"Time": "time_s", "X-axis": "X", "Y-axis": "Y", "Z-axis": "Z"}
    frame = frame.rename(columns=aliases)
    missing = {"time_s", "X", "Y", "Z"} - set(frame.columns)
    if missing:
        raise ValueError(f"Sensor CSV 缺少欄位：{', '.join(sorted(missing))}")
    frame = frame[["time_s", "X", "Y", "Z"]].apply(pd.to_numeric, errors="coerce").dropna()
    frame["time_s"] -= frame["time_s"].iloc[0]
    frame.attrs["metadata"] = metadata
    return frame


def load_status_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {"elapsed_s", "pickup_mode"}
    if not required <= set(frame.columns):
        raise ValueError("狀態 CSV 必須包含 elapsed_s 與 pickup_mode")
    return frame.sort_values("elapsed_s")


class SessionRecorder:
    columns = ["timestamp", "elapsed_s", "pickup_mode", "error", "latency_ms", "host", "port"]

    def __init__(self, root: Path, metadata: dict[str, object]) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.folder = root / f"session_{stamp}"
        self.folder.mkdir(parents=True, exist_ok=False)
        self.csv_path = self.folder / "gpst_status.csv"
        self.metadata_path = self.folder / "session.json"
        self.events_path = self.folder / "events.csv"
        self._stream = self.csv_path.open("w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._stream, fieldnames=self.columns)
        self._writer.writeheader()
        self._event_stream = self.events_path.open("w", newline="", encoding="utf-8-sig")
        self._event_writer = csv.DictWriter(self._event_stream, fieldnames=["timestamp", "elapsed_s", "label"])
        self._event_writer.writeheader()
        metadata = {**metadata, "created_at": datetime.now().astimezone().isoformat()}
        self.metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    def append(self, row: dict[str, object]) -> None:
        self._writer.writerow({key: row.get(key, "") for key in self.columns})
        self._stream.flush()

    def close(self) -> None:
        if not self._stream.closed:
            self._stream.close()
        if not self._event_stream.closed:
            self._event_stream.close()

    def mark(self, timestamp: str, elapsed_s: float, label: str) -> None:
        self._event_writer.writerow({"timestamp": timestamp, "elapsed_s": f"{elapsed_s:.6f}", "label": label})
        self._event_stream.flush()


def align_status_to_sensor(sensor: pd.DataFrame, status: pd.DataFrame, sensor_offset_s: float) -> pd.DataFrame:
    """Attach the most recent GPST sample to every Sensor sample."""
    left = sensor.copy()
    left["sensor_time_s"] = left["time_s"] + sensor_offset_s
    right = status[["elapsed_s", "pickup_mode"]].copy().sort_values("elapsed_s")
    right = right.rename(columns={"elapsed_s": "gpst_time_s"})
    aligned = pd.merge_asof(
        left.sort_values("sensor_time_s"), right,
        left_on="sensor_time_s", right_on="gpst_time_s", direction="backward",
    )
    aligned["gpst_age_s"] = aligned["sensor_time_s"] - aligned["gpst_time_s"]
    aligned.loc[aligned["gpst_time_s"].isna(), "gpst_age_s"] = np.nan
    return aligned[["sensor_time_s", "time_s", "X", "Y", "Z", "pickup_mode", "gpst_time_s", "gpst_age_s"]]
