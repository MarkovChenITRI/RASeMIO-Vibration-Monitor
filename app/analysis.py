"""Status-aware vibration analysis exposed through one public service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

import numpy as np
import pandas as pd
from scipy.signal import butter, find_peaks, resample_poly, sosfiltfilt, spectrogram, welch


@dataclass(frozen=True)
class ProcessingSettings:
    cutoff_hz: float = 200.0
    target_hz: float = 512.0
    threshold_g: float = 0.3
    debounce_samples: int = 3

    def validate(self, source_hz: float) -> None:
        if isinstance(self.debounce_samples, bool) or int(self.debounce_samples) != self.debounce_samples or self.debounce_samples < 1:
            raise ValueError("Debounce samples must be a positive integer")
        if self.cutoff_hz <= 0 or self.target_hz <= 0 or self.threshold_g <= 0:
            raise ValueError("低通截止頻率與重採樣頻率必須大於 0")
        if self.cutoff_hz >= source_hz / 2:
            raise ValueError(f"低通截止頻率必須小於原始 Nyquist 頻率 {source_hz / 2:g} Hz")
        if self.cutoff_hz >= self.target_hz / 2:
            raise ValueError(f"低通截止頻率必須小於目標 Nyquist 頻率 {self.target_hz / 2:g} Hz")


@dataclass
class AnalysisResult:
    aligned: pd.DataFrame
    processed: pd.DataFrame
    metrics: pd.DataFrame
    has_overlap: bool
    covered_samples: int
    selected_samples: int
    unknown_samples: int
    total_samples: int


@dataclass
class FrequencyResult:
    spectra: dict[str, pd.DataFrame]
    bands: pd.DataFrame
    peaks: pd.DataFrame
    spectrograms: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]


@dataclass
class DiagnosisResult:
    events: pd.DataFrame
    evidence: tuple[tuple[str, str, str, str], ...]
    coverage: str


@dataclass
class STFTResult:
    frequency_hz: np.ndarray
    time_s: np.ndarray
    power_g2_hz: np.ndarray
    hotspots: pd.DataFrame


def resolve_sensor_timeline(relative_seconds: pd.Series, local_start: datetime) -> pd.Series:
    if local_start.tzinfo is None:
        local_start = local_start.astimezone()
    start_utc = pd.Timestamp(local_start).tz_convert("UTC")
    return pd.Series(start_utc + pd.to_timedelta(relative_seconds.to_numpy(), unit="s"), index=relative_seconds.index)


def infer_source_hz(frame: pd.DataFrame) -> float:
    dt = float(frame.time_s.diff().dropna().median())
    if not np.isfinite(dt) or dt <= 0: raise ValueError("Sensor CSV 時間欄無法推算取樣頻率")
    return 1.0 / dt


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def _process_segments(frame: pd.DataFrame, selected: np.ndarray, settings: ProcessingSettings,
                      source_hz: float) -> pd.DataFrame:
    pieces = []
    ratio = Fraction(settings.target_hz / source_hz).limit_denominator(10_000)
    sos = butter(4, settings.cutoff_hz, btype="low", fs=source_hz, output="sos")
    segment_id = 0
    for begin, end in _runs(selected):
        candidate = frame.iloc[begin:end]
        gap_edges = np.flatnonzero(candidate.time_s.diff().to_numpy() > 1.5 / source_hz)
        boundaries = [0, *gap_edges.tolist(), len(candidate)]
        for local_begin, local_end in zip(boundaries, boundaries[1:]):
            part = candidate.iloc[local_begin:local_end]
            if len(part) <= 20:
                continue
            values = part[["X", "Y", "Z"]].to_numpy()
            filtered = sosfiltfilt(sos, values, axis=0)
            sampled = resample_poly(filtered, ratio.numerator, ratio.denominator, axis=0)
            duration = (len(part) - 1) / source_hz
            rel = np.linspace(float(part.time_s.iloc[0]), float(part.time_s.iloc[0]) + duration, len(sampled))
            out = pd.DataFrame(sampled, columns=["X", "Y", "Z"])
            out.insert(0, "time_s", rel); out["segment"] = segment_id
            pieces.append(out); segment_id += 1
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(columns=["time_s", "X", "Y", "Z", "segment"])


def _metrics(processed: pd.DataFrame, target_hz: float, threshold_g: float,
             debounce_samples: int = 3) -> pd.DataFrame:
    rows = []
    for axis in "XYZ":
        values = processed[axis].to_numpy(dtype=float)
        absolute = np.abs(values); exceeded = absolute >= threshold_g
        events = longest = total_event_samples = 0
        for _, segment in processed.assign(exceeded=exceeded).groupby("segment", sort=False):
            mask = segment.exceeded.to_numpy(bool)
            runs = [(begin, end) for begin, end in _runs(mask)
                    if end - begin >= debounce_samples]
            lengths = [end - begin for begin, end in runs]
            events += len(runs); total_event_samples += sum(lengths)
            longest = max([longest, *lengths])
        total = len(values); exceed = int(exceeded.sum())
        rows.append({"axis": axis, "RMS_g": float(np.sqrt(np.mean(values**2))) if total else np.nan,
                     "median_abs_g": float(np.median(absolute)) if total else np.nan,
                     "crest_factor": float(absolute.max() / np.sqrt(np.mean(values**2))) if total and np.any(values) else np.nan,
                     "P99_abs_g": float(np.percentile(absolute, 99)) if total else np.nan,
                     "P99.9_abs_g": float(np.percentile(absolute, 99.9)) if total else np.nan,
                     "Max_abs_g": float(absolute.max()) if total else np.nan,
                     "limit_margin_g": float(threshold_g - absolute.max()) if total else np.nan,
                     "peak_to_peak_g": float(values.max() - values.min()) if total else np.nan,
                     "sampling_rate_hz": target_hz, "threshold_g": threshold_g,
                     "debounce_samples": debounce_samples,
                     "within_samples": total - exceed, "exceed_samples": exceed, "total_samples": total,
                     "within_pct": 100 * (total - exceed) / total if total else np.nan,
                     "exceed_pct": 100 * exceed / total if total else np.nan,
                     "event_count": events,
                     "logged_seconds": (float(processed.time_s.max()) - float(processed.time_s.min())) if total > 1 else 0.0,
                     "analyzed_seconds": total / target_hz,
                     "events_per_min": events / (total / target_hz / 60) if total else np.nan,
                     "max_event_ms": longest / target_hz * 1000,
                     "total_excursion_ms": total_event_samples / target_hz * 1000})
    return pd.DataFrame(rows).set_index("axis")


def metrics_for_range(processed: pd.DataFrame, start_s: float, end_s: float,
                      target_hz: float, threshold_g: float = 0.3,
                      debounce_samples: int = 3) -> pd.DataFrame:
    """Calculate metrics for an inclusive sensor-time range."""
    low, high = sorted((float(start_s), float(end_s)))
    selected = processed.loc[processed.time_s.between(low, high)].copy()
    return _metrics(selected, target_hz, threshold_g, debounce_samples)


def diagnose_range(aligned: pd.DataFrame, processed: pd.DataFrame, start_s: float, end_s: float,
                   target_hz: float, threshold_g: float, debounce_samples: int = 3) -> DiagnosisResult:
    """Build cautious, testable clues; never claim a vibration root cause."""
    low, high = sorted((float(start_s), float(end_s)))
    selected = processed.loc[processed.time_s.between(low, high)].copy()
    event_rows = []
    for axis in "XYZ":
        for segment_id, segment in selected.groupby("segment", sort=False):
            values = segment[axis].to_numpy(float); mask = np.abs(values) >= threshold_g
            for begin, end in _runs(mask):
                if end - begin < debounce_samples: continue
                block = segment.iloc[begin:end]; peak_index = block[axis].abs().idxmax()
                peak_time = float(segment.loc[peak_index, "time_s"])
                nearest = aligned.iloc[(aligned.time_s - peak_time).abs().to_numpy().argmin()]
                event_rows.append({
                    "time_s": peak_time, "axis": axis, "peak_g": float(segment.loc[peak_index, axis]),
                    "duration_ms": (end - begin) / target_hz * 1000,
                    "mode": nearest.get("mode", np.nan), "motion_status": nearest.get("motion_status", np.nan),
                    "selected_gripper": nearest.get("selected_gripper", np.nan),
                    "commanded_position": nearest.get("commanded_position", None),
                })
    events = pd.DataFrame(event_rows)
    status_columns = ("motion_status","selected_gripper","commanded_joints","encoder_joints","commanded_position")
    available = [column for column in status_columns if column in aligned and aligned[column].notna().any()]
    coverage = f"{len(available)}/{len(status_columns)} 項診斷狀態有對時資料"
    if events.empty:
        return DiagnosisResult(events, (("目前結果","選定範圍沒有形成有效超標事件","—","維持相同條件重複量測"),), coverage)

    def count_state(column: str, value: int) -> int:
        return int((pd.to_numeric(events.get(column), errors="coerce") == value).sum()) if column in events else 0

    moving, stopped = count_state("motion_status", 0), count_state("motion_status", 1)
    total = len(events)
    path_text = f"{moving}/{total} 個事件發生於 GMST 動作中" if "motion_status" in available else "缺少 GMST 對時資料"
    drive_text = f"{stopped}/{total} 個事件發生於 GMST 停止狀態" if "motion_status" in available else "缺少 GMST 與關節對時資料"
    grippers = pd.to_numeric(events.get("selected_gripper"), errors="coerce").dropna() if "selected_gripper" in events else pd.Series(dtype=float)
    concentration = int(grippers.value_counts().iloc[0]) if not grippers.empty else 0
    mechanism_text = (f"{concentration}/{total} 個事件集中於同一牙叉" if not grippers.empty
                      else "缺少 GSID／位置對時資料")
    evidence = (
        ("路徑／動作規劃", path_text, "中" if moving else "不足", "只降低該路段速度或延長加減速時間後重測"),
        ("驅動參數", drive_text, "中" if stopped else "不足", "保持路徑不變，只改一項濾波或增益並重測"),
        ("機構", mechanism_text, "中" if concentration >= 2 else "不足", "相同姿態比較上手／下手並做至少三次重複測試"),
    )
    return DiagnosisResult(events.sort_values("time_s"), evidence, coverage)


def frequency_analysis(processed: pd.DataFrame, start_s: float, end_s: float,
                       sample_hz: float, cutoff_hz: float,
                       bands: tuple[tuple[str, float, float], ...] | None = None) -> FrequencyResult:
    """Calculate Welch PSD, band energy, dominant peaks and spectrogram data."""
    low, high = sorted((float(start_s), float(end_s)))
    selected = processed.loc[processed.time_s.between(low, high)].copy()
    if len(selected) < 32:
        raise ValueError("頻域分析至少需要 32 個有效樣本")
    upper = min(float(cutoff_hz), sample_hz / 2)
    configured = bands or (("低頻", .5, 10.0), ("中頻", 10.0, 50.0), ("高頻", 50.0, upper))
    configured = tuple((name, begin, min(end, upper)) for name, begin, end in configured if begin < min(end, upper))
    spectra, spectrograms, band_rows, peak_rows = {}, {}, [], []
    for axis in "XYZ":
        raw_values = selected[axis].to_numpy(dtype=float)
        values = raw_values - np.mean(raw_values)
        frequency, density = welch(values, fs=sample_hz, nperseg=min(2048, len(values)),
                                   window="hann", scaling="density")
        keep = frequency <= upper
        frequency, density = frequency[keep], density[keep]
        spectra[axis] = pd.DataFrame({"frequency_hz": frequency, "psd_g2_hz": density})
        total_power = float(np.trapezoid(density, frequency)) if len(frequency) > 1 else 0.0
        for name, begin, end in configured:
            mask = (frequency >= begin) & (frequency < end if end < upper else frequency <= end)
            power = float(np.trapezoid(density[mask], frequency[mask])) if mask.sum() > 1 else 0.0
            band_rows.append({"axis": axis, "band": name, "range_hz": f"{begin:g}–{end:g}",
                              "power_g2": power, "rms_g": np.sqrt(max(power, 0)),
                              "power_pct": 100 * power / total_power if total_power > 0 else np.nan})
        peak_indices, _ = find_peaks(density, prominence=max(float(density.max()) * .01, np.finfo(float).eps))
        for rank, index in enumerate(peak_indices[np.argsort(density[peak_indices])[::-1]][:5], 1):
            peak_rows.append({"axis": axis, "rank": rank, "frequency_hz": frequency[index],
                              "psd_g2_hz": density[index]})
        spec_n = min(1024, len(values))
        f_spec, t_spec, power_spec = spectrogram(values, fs=sample_hz, nperseg=spec_n,
                                                 noverlap=spec_n // 2, scaling="density", mode="psd")
        spec_keep = f_spec <= upper
        spectrograms[axis] = (f_spec[spec_keep], t_spec + low, power_spec[spec_keep])
    return FrequencyResult(spectra, pd.DataFrame(band_rows), pd.DataFrame(peak_rows), spectrograms)


def stft_analysis(processed: pd.DataFrame, start_s: float, end_s: float, sample_hz: float,
                  axis: str, minimum_hz: float, maximum_hz: float,
                  window_seconds: float, overlap_pct: float) -> STFTResult:
    if axis not in "XYZ": raise ValueError("STFT 軸向必須是 X、Y 或 Z")
    if not 0 <= overlap_pct < 100: raise ValueError("STFT 重疊率必須介於 0% 到 100% 之間")
    if window_seconds <= 0 or minimum_hz < 0 or maximum_hz <= minimum_hz:
        raise ValueError("STFT 頻率與時間窗設定無效")
    selected=processed.loc[processed.time_s.between(*sorted((start_s,end_s)))].copy()
    if len(selected)<32: raise ValueError("STFT 至少需要 32 個有效樣本")
    nperseg=min(len(selected),max(32,int(round(window_seconds*sample_hz))))
    noverlap=min(nperseg-1,int(round(nperseg*overlap_pct/100)))
    frequency,times,power=spectrogram(selected[axis].to_numpy(float)-selected[axis].mean(),fs=sample_hz,
                                      nperseg=nperseg,noverlap=noverlap,scaling="density",mode="psd")
    keep=(frequency>=minimum_hz)&(frequency<=min(maximum_hz,sample_hz/2))
    frequency,power=frequency[keep],power[keep]
    times=times+float(selected.time_s.iloc[0])
    if not len(frequency): raise ValueError("STFT 設定範圍內沒有可分析的頻率")
    flat=np.argsort(power.ravel())[::-1][:min(10,power.size)]
    rows=[]
    for rank,index in enumerate(flat,1):
        f_index,t_index=np.unravel_index(index,power.shape)
        rows.append({"rank":rank,"time_s":times[t_index],"frequency_hz":frequency[f_index],"power_g2_hz":power[f_index,t_index]})
    return STFTResult(frequency,times,power,pd.DataFrame(rows))


def iso10816_reference(processed: pd.DataFrame, start_s: float, end_s: float, sample_hz: float,
                       minimum_hz: float, maximum_hz: float,
                       boundaries: tuple[float,float,float]) -> pd.DataFrame:
    """Calculate band-limited velocity RMS; classification limits are user supplied."""
    if minimum_hz <= 0 or maximum_hz <= minimum_hz or maximum_hz > sample_hz/2:
        raise ValueError("ISO 10816 參考頻帶必須大於 0 且不超過 Nyquist 頻率")
    if len(boundaries)!=3 or not 0<boundaries[0]<boundaries[1]<boundaries[2]:
        raise ValueError("ISO 10816 A/B、B/C、C/D 分界值必須依序遞增")
    selected=processed.loc[processed.time_s.between(*sorted((start_s,end_s)))].copy()
    if len(selected)<32: raise ValueError("ISO 10816 參考計算至少需要 32 個有效樣本")
    rows=[]; g0=9.80665
    for axis in "XYZ":
        frequency,accel_psd=welch(selected[axis].to_numpy(float)-selected[axis].mean(),fs=sample_hz,nperseg=min(2048,len(selected)),scaling="density")
        keep=(frequency>=minimum_hz)&(frequency<=maximum_hz)
        velocity_psd=accel_psd[keep]*g0**2/(2*np.pi*frequency[keep])**2
        velocity_rms=float(np.sqrt(max(np.trapezoid(velocity_psd,frequency[keep]),0))*1000) if keep.sum()>1 else 0.0
        zone="A" if velocity_rms<boundaries[0] else "B" if velocity_rms<boundaries[1] else "C" if velocity_rms<boundaries[2] else "D"
        rows.append({"axis":axis,"velocity_rms_mm_s":velocity_rms,"zone":zone})
    return pd.DataFrame(rows).set_index("axis")


def status_mask_intervals(observations: pd.DataFrame, local_start: datetime, polling_interval_s: float,
                          status_offset_s: float = 0.0, column: str = "mode") -> list[tuple[float, float, int]]:
    """Map status samples onto sensor seconds as (begin, end, value) runs.

    Each sample stays valid until the next sample or the same staleness tolerance
    used by analyze_recording, so the mask matches the samples the analysis selects.
    """
    if observations.empty or column not in observations:
        return []
    if local_start.tzinfo is None:
        local_start = local_start.astimezone()
    start_utc = pd.Timestamp(local_start).tz_convert("UTC")
    frame = observations[["timestamp", column]].copy()
    frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True)
    frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna().sort_values("timestamp")
    if frame.empty:
        return []
    seconds = (frame.timestamp - start_utc).dt.total_seconds().to_numpy(float) + float(status_offset_s)
    values = frame[column].to_numpy(float).astype(int)
    tolerance = 3 * polling_interval_s
    ends = np.minimum(np.r_[seconds[1:], np.inf], seconds + tolerance)
    intervals: list[tuple[float, float, int]] = []
    for begin, end, value in zip(seconds, ends, values):
        if intervals and intervals[-1][2] == value and begin <= intervals[-1][1] + 1e-9:
            intervals[-1] = (intervals[-1][0], float(end), int(value))
        else:
            intervals.append((float(begin), float(end), int(value)))
    return intervals


def analyze_recording(sensor: pd.DataFrame, local_start: datetime, observations: pd.DataFrame,
                      selected_modes: set[int], settings: ProcessingSettings,
                      polling_interval_s: float, filters: dict | None = None,
                      status_offset_s: float = 0.0) -> AnalysisResult:
    """status_offset_s shifts RASeMIO status later (+) or earlier (-) on the sensor timeline."""
    source_hz = infer_source_hz(sensor); settings.validate(source_hz)
    aligned = sensor.copy(); aligned["timestamp"] = resolve_sensor_timeline(sensor.time_s, local_start)
    if observations.empty:
        aligned["mode"] = np.nan; aligned["status_timestamp"] = pd.NaT
    else:
        right = observations.copy()
        right["timestamp"] = (pd.to_datetime(right.timestamp, utc=True).astype("datetime64[ns, UTC]")
                              + pd.Timedelta(seconds=float(status_offset_s)))
        aligned["timestamp"] = aligned["timestamp"].astype("datetime64[ns, UTC]")
        right = right.sort_values("timestamp").rename(columns={"timestamp": "status_timestamp"})
        aligned = pd.merge_asof(aligned.sort_values("timestamp"), right,
                                left_on="timestamp", right_on="status_timestamp", direction="backward",
                                tolerance=pd.Timedelta(seconds=3 * polling_interval_s))
    aligned["covered"] = aligned["mode"].notna()
    has_overlap = bool(aligned["covered"].any())
    selected=np.ones(len(aligned),dtype=bool)
    if has_overlap:
        if selected_modes:selected&=aligned["mode"].isin(selected_modes).to_numpy()
        for column,rule in (filters or {}).items():
            if column not in aligned:continue
            if "values" in rule and rule["values"]:
                selected&=pd.to_numeric(aligned[column],errors="coerce").isin(rule["values"]).to_numpy()
            numeric=pd.to_numeric(aligned[column],errors="coerce")
            if rule.get("minimum") is not None:selected&=numeric.ge(rule["minimum"]).to_numpy()
            if rule.get("maximum") is not None:selected&=numeric.le(rule["maximum"]).to_numpy()
    aligned["selected"] = selected
    mask = aligned.selected.to_numpy(bool)
    if filters and has_overlap and not mask.any():
        raise ValueError("目前篩選條件在 CSV 對時範圍內沒有任何資料")
    processed = _process_segments(aligned, mask, settings, source_hz)
    return AnalysisResult(aligned, processed, _metrics(processed, settings.target_hz, settings.threshold_g,
                                                       settings.debounce_samples),
                          has_overlap, int(aligned.covered.sum()), int(mask.sum()),
                          int((~aligned.covered).sum()), len(aligned))
