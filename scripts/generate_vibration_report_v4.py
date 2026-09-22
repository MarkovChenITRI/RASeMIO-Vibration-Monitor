"""Generate WR503 V4 PDF with auditable numerator/denominator improvement tables."""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_vibration_report_v2 as base


OUTPUT = base.ROOT / "reports" / "WR503_vibration_standardized_report_v4.pdf"
METRICS = [
    ("RMS_g", "RMS", "g"),
    ("P99_abs_g", "|g| P99", "g"),
    ("Max_abs_g", "|g| maximum", "g"),
    ("Exceed_pct", "Samples >= 0.3 g", "%"),
    ("Events_per_min", "Exceedance events", "events/min"),
]


def _metric_record(frame: pd.DataFrame, axis: str) -> dict[str, float]:
    values = frame[axis].to_numpy()
    absolute = np.abs(values)
    exceeded = absolute >= base.THRESHOLD
    starts = exceeded & ~np.r_[False, exceeded[:-1]]
    duration_s = len(frame) / base.TARGET_FS
    return {
        "RMS_g": float(np.sqrt(np.mean(values**2))),
        "P99_abs_g": float(np.percentile(absolute, 99)),
        "Max_abs_g": float(absolute.max()),
        "Exceed_pct": float(100 * exceeded.mean()),
        "Events_per_min": float(starts.sum() / (duration_s / 60)),
        "event_count": int(starts.sum()),
        "exceed_samples": int(exceeded.sum()),
        "total_samples": int(len(frame)),
        "duration_s": float(duration_s),
    }


def improvement_table(
    series: dict[str, pd.DataFrame], baseline_labels: list[str], current_label: str
) -> pd.DataFrame:
    labels = baseline_labels + [current_label]
    common_samples = min(len(series[label]) for label in labels)
    records = {
        label: {axis: _metric_record(series[label].iloc[:common_samples], axis) for axis in "XYZ"}
        for label in labels
    }
    rows: list[dict[str, object]] = []
    for axis in "XYZ":
        row: dict[str, object] = {
            "axis": axis,
            "baseline_labels": tuple(baseline_labels),
            "current_label": current_label,
            "duration_s": records[current_label][axis]["duration_s"],
            "total_samples": records[current_label][axis]["total_samples"],
        }
        for metric, _, _ in METRICS:
            raw = [records[label][axis][metric] for label in baseline_labels]
            old = float(np.mean(raw))
            new = float(records[current_label][axis][metric])
            row[f"{metric}_baseline_raw"] = tuple(raw)
            row[f"{metric}_benchmark"] = old
            row[f"{metric}_aug"] = new
            row[f"{metric}_improvement"] = 100 * (old - new) / old if old else np.nan
        row["event_count_baseline_raw"] = tuple(
            records[label][axis]["event_count"] for label in baseline_labels
        )
        row["event_count_aug"] = records[current_label][axis]["event_count"]
        row["exceed_samples_baseline_raw"] = tuple(
            records[label][axis]["exceed_samples"] for label in baseline_labels
        )
        row["exceed_samples_aug"] = records[current_label][axis]["exceed_samples"]
        rows.append(row)
    return pd.DataFrame(rows).set_index("axis")


def _fmt(value: float, unit: str) -> str:
    if unit == "g":
        return f"{value:.5f} g"
    if unit == "%":
        return f"{value:.5f}%"
    return f"{value:.3f}/min"


def improvement_page(pdf, title: str, result: pd.DataFrame) -> None:
    """Replace percentage-only charts with values and explicit calculation direction."""
    labels = result.iloc[0]["baseline_labels"]
    duration = float(result.iloc[0]["duration_s"])
    samples = int(result.iloc[0]["total_samples"])
    rows: list[list[str]] = []
    for axis, record in result.iterrows():
        for metric, metric_label, unit in METRICS:
            raw = record[f"{metric}_baseline_raw"]
            benchmark = float(record[f"{metric}_benchmark"])
            august = float(record[f"{metric}_aug"])
            change = float(record[f"{metric}_improvement"])
            raw_text = " / ".join(_fmt(float(value), unit) for value in raw)
            if metric == "Events_per_min":
                counts = record["event_count_baseline_raw"]
                raw_text = " / ".join(
                    f"{count} events ({float(rate):.3f}/min)"
                    for count, rate in zip(counts, raw)
                )
                august_text = f"{int(record['event_count_aug'])} events ({august:.3f}/min)"
            elif metric == "Exceed_pct":
                counts = record["exceed_samples_baseline_raw"]
                raw_text = " / ".join(
                    f"{count}/{samples} ({float(rate):.5f}%)"
                    for count, rate in zip(counts, raw)
                )
                august_text = f"{int(record['exceed_samples_aug'])}/{samples} ({august:.5f}%)"
            else:
                august_text = _fmt(august, unit)
            direction = "improved" if change >= 0 else "worsened"
            formula = (
                f"({_fmt(benchmark, unit)} - {_fmt(august, unit)}) / "
                f"{_fmt(benchmark, unit)} = {change:+.2f}% ({direction})"
            )
            rows.append([axis, metric_label, raw_text, _fmt(benchmark, unit), august_text, formula])

    fig = plt.figure(figsize=(11.69, 8.27))
    baseline_note = "single run" if len(labels) == 1 else "mean of WPH249 and WPH250; both raw values shown"
    base.page_header(
        fig,
        title + " — auditable change calculation",
        f"Common window: {duration:.3f} s, {samples:,} samples at {base.TARGET_FS:.0f} Hz; baseline = {baseline_note}",
    )
    ax = fig.add_axes([0.025, 0.055, 0.95, 0.79])
    ax.axis("off")
    table = ax.table(
        cellText=rows,
        colLabels=["Axis", "Metric", "Baseline raw run(s)", "Baseline denominator", "2026-08-27 numerator", "Formula and result"],
        cellLoc="center",
        loc="upper center",
        colWidths=[0.04, 0.105, 0.22, 0.13, 0.16, 0.345],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(6.2)
    table.scale(1, 1.42)
    for (row, _), cell in table.get_celld().items():
        cell.set_edgecolor("#BCC7CC")
        cell.set_linewidth(0.35)
        if row == 0:
            cell.set_facecolor("#18354A")
            cell.set_text_props(color="white", weight="bold")
        elif row % 2 == 0:
            cell.set_facecolor("#EDF2F4")
    pdf.savefig(fig)
    plt.close(fig)


_original_dataframe_page = base.dataframe_page


def dataframe_page(pdf, title, subtitle, table, formats):
    """Remove the redundant percentage-only summary; detailed pages are authoritative."""
    if any("improvement (%)" in str(column) for column in table.columns):
        base.text_page(
            pdf,
            "V4 calculation traceability summary",
            [
                (
                    "Authoritative result pages",
                    "The four preceding comparison tables show every X/Y/Z result as baseline raw value(s), "
                    "baseline denominator, August numerator, and (baseline - August) / baseline. "
                    "For June, WPH249 and WPH250 are displayed separately before their mean is used.",
                ),
                (
                    "Event and threshold counts",
                    "Exceedance events are reported as true counts together with the common observation duration "
                    "and events/min. Threshold sample rates show exceeded samples / total samples. Positive values "
                    "mean improvement; negative values mean worsening.",
                ),
            ],
        )
        return
    _original_dataframe_page(pdf, title, subtitle, table, formats)


def main() -> None:
    base.OUTPUT = OUTPUT
    base.improvement_table = improvement_table
    base.improvement_page = improvement_page
    base.dataframe_page = dataframe_page
    base.main()


if __name__ == "__main__":
    main()
