"""Audit WR503 vibration files before quantitative cross-date comparison."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal

from generate_vibration_report import DATASETS, load_recdata


FILTERED = {
    "2026-08-27 上手 J5 濾波後": Path(r"C:\ITRI\歐陽\SAA\WR503\震動量測\20260827_vib test\濾波後\上手J5\RecData--20260827134954.csv"),
    "2026-08-27 下手 J4 濾波後": Path(r"C:\ITRI\歐陽\SAA\WR503\震動量測\20260827_vib test\濾波後\下手J4\RecData--20260827134957.csv"),
}
TARGET_FS = 512.0
COMMON_CUTOFF_HZ = 200.0
ENVELOPE_SECONDS = 0.25


def standardize(frame: pd.DataFrame) -> pd.DataFrame:
    dt = float(frame.time_s.diff().median())
    fs = 1 / dt
    sos = signal.butter(4, COMMON_CUTOFF_HZ, btype="lowpass", fs=fs, output="sos")
    values = signal.sosfiltfilt(sos, frame[["X", "Y", "Z"]].to_numpy(), axis=0)
    ratio = fs / TARGET_FS
    if np.isclose(ratio, 2, rtol=0.01):
        values = signal.resample_poly(values, 1, 2, axis=0)
    elif not np.isclose(ratio, 1, rtol=0.01):
        count = int(round(len(values) * TARGET_FS / fs))
        values = signal.resample(values, count, axis=0)
    time_s = np.arange(len(values)) / TARGET_FS
    return pd.DataFrame(values, columns=["X", "Y", "Z"]).assign(time_s=time_s)


def envelope(frame: pd.DataFrame) -> np.ndarray:
    block = int(TARGET_FS * ENVELOPE_SECONDS)
    values = frame[["X", "Y", "Z"]].to_numpy()
    count = len(values) // block
    values = values[: count * block].reshape(count, block, 3)
    return np.sqrt(np.mean(values**2, axis=(1, 2)))


def align(reference: np.ndarray, candidate: np.ndarray) -> tuple[float, float, float]:
    ref = (reference - reference.mean()) / (reference.std() or 1)
    cand = (candidate - candidate.mean()) / (candidate.std() or 1)
    correlation = signal.correlate(cand, ref, mode="full", method="fft")
    overlap = signal.correlate(np.ones_like(cand), np.ones_like(ref), mode="full", method="fft")
    normalized = correlation / np.maximum(overlap, 1)
    lags = signal.correlation_lags(len(cand), len(ref), mode="full")
    valid = overlap >= 0.7 * min(len(ref), len(cand))
    index = np.flatnonzero(valid)[np.argmax(normalized[valid])]
    lag_seconds = lags[index] * ENVELOPE_SECONDS
    overlap_seconds = overlap[index] * ENVELOPE_SECONDS
    return float(lag_seconds), float(normalized[index]), float(overlap_seconds)


def main() -> None:
    all_paths = DATASETS | FILTERED
    raw = {}
    meta = {}
    audit_rows = []
    for label, path in all_paths.items():
        frame, metadata = load_recdata(path)
        raw[label], meta[label] = frame, metadata
        dt = float(frame.time_s.diff().median())
        fs = 1 / dt
        deltas = frame.time_s.diff().dropna()
        expected = int(round((frame.time_s.iloc[-1] - frame.time_s.iloc[0]) * fs)) + 1
        audit_rows.append(
            {
                "dataset": label,
                "declared_start": metadata.get("Date/Time", "missing"),
                "declared_fs": metadata.get("SamplingRate", "missing"),
                "inferred_fs": fs,
                "samples": len(frame),
                "duration_s": frame.time_s.iloc[-1] - frame.time_s.iloc[0],
                "missing_by_timeline": max(0, expected - len(frame)),
                "duplicate_timestamps": int(frame.time_s.duplicated().sum()),
                "nonpositive_steps": int((deltas <= 0).sum()),
                "large_gaps": int((deltas > 1.5 * dt).sum()),
                "processing_label": "filtered" if "濾波後" in label else "raw/unfiltered",
            }
        )
    audit = pd.DataFrame(audit_rows).set_index("dataset")
    standardized = {label: standardize(frame) for label, frame in raw.items()}
    envelopes = {label: envelope(frame) for label, frame in standardized.items()}

    pairs = {
        "0326 upper vs 0827 raw upper": ("2026-08-27 上手 J5", "2026-03-26 上手"),
        "0326 lower vs 0827 raw lower": ("2026-08-27 下手 J4", "2026-03-26 下手"),
        "0603 WPH249 upper vs 0827 raw upper": ("2026-08-27 上手 J5", "2026-06-03 WPH249 上手"),
        "0603 WPH250 upper vs 0827 raw upper": ("2026-08-27 上手 J5", "2026-06-03 WPH250 上手"),
        "0603 WPH249 lower vs 0827 raw lower": ("2026-08-27 下手 J4", "2026-06-03 WPH249 下手"),
        "0603 WPH250 lower vs 0827 raw lower": ("2026-08-27 下手 J4", "2026-06-03 WPH250 下手"),
        "0827 filtered vs raw upper": ("2026-08-27 上手 J5", "2026-08-27 上手 J5 濾波後"),
        "0827 filtered vs raw lower": ("2026-08-27 下手 J4", "2026-08-27 下手 J4 濾波後"),
    }
    alignment_rows = []
    for name, (reference, candidate) in pairs.items():
        lag, corr, overlap = align(envelopes[reference], envelopes[candidate])
        alignment_rows.append({"comparison": name, "candidate_lag_s": lag, "envelope_correlation": corr, "overlap_s": overlap})
    alignment = pd.DataFrame(alignment_rows).set_index("comparison")

    output = Path(__file__).resolve().parents[1] / "reports"
    output.mkdir(exist_ok=True)
    audit.to_csv(output / "data_comparability_audit.csv", encoding="utf-8-sig")
    alignment.to_csv(output / "process_alignment_audit.csv", encoding="utf-8-sig")
    print("\nFILE AUDIT\n", audit.to_string())
    print("\nPROCESS ALIGNMENT AUDIT\n", alignment.to_string())


if __name__ == "__main__":
    main()
