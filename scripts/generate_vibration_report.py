"""Generate a publication-style PDF report for WR503 vibration comparisons."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.ticker import MultipleLocator


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "WR503_vibration_comparison_report_v2.pdf"
THRESHOLD = 0.3
LOAD_WAFERS = 250
REQUIRED_WAFERS = 265
DISPLAY_POINTS = 70_000

DATASETS = {
    "2026-03-26 上手": Path(r"C:\ITRI\歐陽\SAA\WR503\震動量測\20260326\上手\RecData--20260326133043上手.csv"),
    "2026-03-26 下手": Path(r"C:\ITRI\歐陽\SAA\WR503\震動量測\20260326\下手\RecData--20260326133043下手.csv"),
    "2026-06-03 WPH249 上手": Path(r"C:\ITRI\歐陽\SAA\WR503\震動量測\20260603\WPH249\上手\RecData--20260603142758.csv"),
    "2026-06-03 WPH249 下手": Path(r"C:\ITRI\歐陽\SAA\WR503\震動量測\20260603\WPH249\下手\RecData--20260603142937.csv"),
    "2026-06-03 WPH250 上手": Path(r"C:\ITRI\歐陽\SAA\WR503\震動量測\20260603\WPH250\上手\RecData--20260603153648.csv"),
    "2026-06-03 WPH250 下手": Path(r"C:\ITRI\歐陽\SAA\WR503\震動量測\20260603\WPH250\下手\RecData--20260603153829.csv"),
    "2026-08-27 上手 J5": Path(r"C:\ITRI\歐陽\SAA\WR503\震動量測\20260827_vib test\濾波前\上手J5\RecData--20260827115016.csv"),
    "2026-08-27 下手 J4": Path(r"C:\ITRI\歐陽\SAA\WR503\震動量測\20260827_vib test\濾波前\下手J4\RecData--20260827115019.csv"),
}

COMPARISONS = {
    "上手：3月基準 vs 8月調整後": ["2026-03-26 上手", "2026-08-27 上手 J5"],
    "下手：3月基準 vs 8月調整後": ["2026-03-26 下手", "2026-08-27 下手 J4"],
    "上手：6月基準 vs 8月調整後": ["2026-06-03 WPH249 上手", "2026-06-03 WPH250 上手", "2026-08-27 上手 J5"],
    "下手：6月基準 vs 8月調整後": ["2026-06-03 WPH249 下手", "2026-06-03 WPH250 下手", "2026-08-27 下手 J4"],
}

COLORS = ["#315A7D", "#8A9A5B", "#C45A3C"]


def setup_style() -> None:
    font_file = Path(r"C:\Windows\Fonts\msjh.ttc")
    if font_file.exists():
        font_manager.fontManager.addfont(font_file)
        family = font_manager.FontProperties(fname=font_file).get_name()
        plt.rcParams["font.family"] = family
    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.titlesize": 12,
            "axes.labelsize": 9,
            "axes.linewidth": 0.8,
            "axes.unicode_minus": False,
            "figure.facecolor": "white",
            "savefig.facecolor": "white",
            "pdf.fonttype": 42,
        }
    )


def load_recdata(path: Path) -> tuple[pd.DataFrame, dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        preview = [stream.readline() for _ in range(30)]
    header = next(i for i, line in enumerate(preview) if "time" in line.lower() and "," in line)
    metadata: dict[str, str] = {}
    for line in preview[:header]:
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    frame = pd.read_csv(path, skiprows=header)
    frame.columns = frame.columns.str.strip()
    frame = frame.rename(columns={"Time": "time_s", "X-axis": "X", "Y-axis": "Y", "Z-axis": "Z"})
    frame = frame[["time_s", "X", "Y", "Z"]].apply(pd.to_numeric, errors="coerce").dropna()
    return frame.sort_values("time_s").drop_duplicates("time_s"), metadata


def longest_run(mask: np.ndarray) -> int:
    transitions = np.diff(np.r_[False, mask, False].astype(np.int8))
    lengths = np.flatnonzero(transitions == -1) - np.flatnonzero(transitions == 1)
    return int(lengths.max()) if len(lengths) else 0


def calculate_metrics(series: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for label, frame in series.items():
        dt = float(frame.time_s.diff().dropna().median())
        for axis in "XYZ":
            values = frame[axis].to_numpy()
            absolute = np.abs(values)
            exceeded = absolute >= THRESHOLD
            starts = exceeded & ~np.r_[False, exceeded[:-1]]
            rows.append(
                {
                    "dataset": label,
                    "axis": axis,
                    "sampling_rate_Hz": 1 / dt,
                    "RMS_g": np.sqrt(np.mean(values**2)),
                    "P99_abs_g": np.percentile(absolute, 99),
                    "P99.9_abs_g": np.percentile(absolute, 99.9),
                    "Max_abs_g": absolute.max(),
                    "Within_pct": 100 * np.mean(absolute < THRESHOLD),
                    "Exceed_pct": 100 * np.mean(exceeded),
                    "Exceed_seconds": exceeded.sum() * dt,
                    "Events_per_min": starts.sum() / (len(values) * dt / 60),
                    "Max_event_ms": longest_run(exceeded) * dt * 1000,
                }
            )
    return pd.DataFrame(rows).set_index(["dataset", "axis"])


def page_header(fig: plt.Figure, title: str, subtitle: str = "") -> None:
    fig.text(0.055, 0.955, title, fontsize=18, weight="bold", color="#18354A", va="top")
    if subtitle:
        fig.text(0.055, 0.918, subtitle, fontsize=9.5, color="#52616B", va="top")
    fig.add_artist(plt.Line2D([0.055, 0.945], [0.9, 0.9], transform=fig.transFigure, color="#C45A3C", lw=1.5))


def text_page(pdf: PdfPages, title: str, paragraphs: list[tuple[str, str]]) -> None:
    fig = plt.figure(figsize=(8.27, 11.69))
    page_header(fig, title)
    y = 0.85
    for heading, body in paragraphs:
        fig.text(0.08, y, heading, fontsize=12, weight="bold", color="#18354A", va="top")
        y -= 0.034
        fig.text(0.08, y, body, fontsize=10, color="#27343B", va="top", wrap=True, linespacing=1.55)
        y -= 0.035 * (body.count("\n") + max(2, len(body) // 50)) + 0.035
    fig.text(0.5, 0.025, "WR503 vibration assessment • confidential engineering report", ha="center", fontsize=7, color="#7A858B")
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def timeseries_page(pdf: PdfPages, title: str, labels: list[str], series: dict[str, pd.DataFrame], y_limit: float) -> None:
    fig, axs = plt.subplots(3, 1, figsize=(11.69, 8.27), sharex=True, sharey=True)
    fig.subplots_adjust(left=0.08, right=0.97, bottom=0.09, top=0.84, hspace=0.14)
    page_header(fig, title, f"原始／濾波前資料；共同 y 軸 ±{y_limit:.1f} g；紅色虛線為廠商門檻 ±{THRESHOLD:.1f} g")
    for axis, ax in zip("XYZ", axs):
        for index, label in enumerate(labels):
            frame = series[label]
            step = max(1, int(np.ceil(len(frame) / DISPLAY_POINTS)))
            view = frame.iloc[::step]
            ax.plot(view.time_s, view[axis], lw=0.45, alpha=0.78, color=COLORS[index], label=label)
        ax.axhline(THRESHOLD, color="#C00000", ls="--", lw=1.0)
        ax.axhline(-THRESHOLD, color="#C00000", ls="--", lw=1.0)
        ax.set_ylim(-y_limit, y_limit)
        ax.yaxis.set_major_locator(MultipleLocator(0.2))
        ax.set_ylabel(f"{axis} acceleration (g)")
        ax.grid(True, color="#D9E0E3", linewidth=0.45)
        ax.spines[["top", "right"]].set_visible(False)
    axs[0].legend(loc="upper right", frameon=True, fontsize=8)
    axs[-1].set_xlabel("Time (s)")
    pdf.savefig(fig)
    plt.close(fig)


def dataframe_page(pdf: PdfPages, title: str, subtitle: str, table: pd.DataFrame, formats: dict[str, str]) -> None:
    shown = table.copy()
    for column, fmt in formats.items():
        shown[column] = shown[column].map(lambda value: fmt.format(value))
    shown = shown.reset_index()
    fig = plt.figure(figsize=(11.69, 8.27))
    page_header(fig, title, subtitle)
    ax = fig.add_axes([0.04, 0.08, 0.92, 0.77])
    ax.axis("off")
    artist = ax.table(cellText=shown.values, colLabels=shown.columns, cellLoc="center", loc="upper center")
    artist.auto_set_font_size(False)
    artist.set_fontsize(7.5)
    artist.scale(1, 1.45)
    artist.auto_set_column_width(col=list(range(len(shown.columns))))
    for (row, _), cell in artist.get_celld().items():
        if row == 0:
            cell.set_facecolor("#18354A")
            cell.set_text_props(color="white", weight="bold")
        elif row % 2 == 0:
            cell.set_facecolor("#EDF2F4")
        cell.set_edgecolor("#BCC7CC")
        cell.set_linewidth(0.4)
    pdf.savefig(fig)
    plt.close(fig)


def improvement_table(metrics: pd.DataFrame, baseline_labels: list[str], current_label: str) -> pd.DataFrame:
    baseline = pd.concat([metrics.loc[label] for label in baseline_labels]).groupby(level=0).mean()
    current = metrics.loc[current_label]
    rows = []
    for axis in "XYZ":
        row = {"axis": axis}
        for metric in ["RMS_g", "P99_abs_g", "Max_abs_g", "Exceed_pct", "Events_per_min"]:
            old, new = baseline.loc[axis, metric], current.loc[axis, metric]
            row[f"{metric}_benchmark"] = old
            row[f"{metric}_aug"] = new
            row[f"{metric}_improvement"] = 100 * (old - new) / old if old else np.nan
        rows.append(row)
    return pd.DataFrame(rows).set_index("axis")


def improvement_page(pdf: PdfPages, title: str, result: pd.DataFrame) -> None:
    metrics = [
        ("RMS_g", "RMS"),
        ("P99_abs_g", "|g| P99"),
        ("Exceed_pct", ">=0.3 g 超標率"),
        ("Events_per_min", "每分鐘超標事件"),
    ]
    fig, axs = plt.subplots(2, 2, figsize=(11.69, 8.27))
    fig.subplots_adjust(left=0.09, right=0.97, bottom=0.1, top=0.82, hspace=0.38, wspace=0.25)
    page_header(fig, title, "改善率 = (benchmark - 2026-08-27) / benchmark；正值為改善，負值為退步")
    for ax, (metric, label) in zip(axs.flat, metrics):
        values = result[f"{metric}_improvement"]
        colors = ["#3A7D44" if value >= 0 else "#B33A3A" for value in values]
        bars = ax.bar(result.index, values, color=colors, width=0.65)
        ax.axhline(0, color="#20272B", lw=0.8)
        ax.bar_label(bars, fmt="%.1f%%", padding=3, fontsize=8)
        ax.set_title(label)
        ax.set_ylabel("Improvement (%)")
        ax.grid(axis="y", color="#D9E0E3", linewidth=0.45)
        ax.spines[["top", "right"]].set_visible(False)
    pdf.savefig(fig)
    plt.close(fig)


def main() -> None:
    setup_style()
    missing = [str(path) for path in DATASETS.values() if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing input files:\n" + "\n".join(missing))
    series: dict[str, pd.DataFrame] = {}
    metadata = {}
    for label, path in DATASETS.items():
        series[label], metadata[label] = load_recdata(path)
    metrics = calculate_metrics(series)
    global_max = max(frame[["X", "Y", "Z"]].abs().to_numpy().max() for frame in series.values())
    y_limit = float(np.ceil(global_max * 1.03 / 0.1) * 0.1)

    march_upper = improvement_table(metrics, ["2026-03-26 上手"], "2026-08-27 上手 J5")
    march_lower = improvement_table(metrics, ["2026-03-26 下手"], "2026-08-27 下手 J4")
    june_upper = improvement_table(metrics, ["2026-06-03 WPH249 上手", "2026-06-03 WPH250 上手"], "2026-08-27 上手 J5")
    june_lower = improvement_table(metrics, ["2026-06-03 WPH249 下手", "2026-06-03 WPH250 下手"], "2026-08-27 下手 J4")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(OUTPUT, metadata={"Title": "WR503 vibration comparison report", "Author": "ITRI"}) as pdf:
        text_page(
            pdf,
            "WR503 Wafer 搬運機器人震動評估報告",
            [
                ("研究目的", "比較2026年7月底機構調整前（3月、6月）與調整後（8月）的上手／下手原始震動資料，量化改善或退步幅度。"),
                ("規格背景", f"廠商目標為搬運 {REQUIRED_WAFERS} 片 wafer 時加速度低於 ±{THRESHOLD:.1f} g；目前資料皆為 {LOAD_WAFERS} 片條件。"),
                ("重要結論", "8月資料並未在全部樣本上滿足 |g| < 0.3 g。相對6月，部分X/Z指標改善，但下手Y的RMS、P99與超標率明顯惡化；相對3月，多數軸向指標退步。"),
                ("判讀限制", "本報告可作為機構調整效果的量化比較，但不能取代265片條件下的正式驗收。廠商仍需確認0.3 g規格適用於瞬時峰值、RMS、特定頻帶或指定時間窗。"),
            ],
        )
        text_page(
            pdf,
            "資料與分析方法",
            [
                ("資料範圍", "3月：上手、下手各一筆。\n6月：WPH249與WPH250，上手、下手各一筆。\n8月：濾波前上手J5、濾波前下手J4各一筆。"),
                ("量化指標", "RMS、|g| P99/P99.9、最大絕對值、低於0.3 g樣本比例、超標累積時間、每分鐘超標事件數及最長連續超標時間。"),
                ("公平比較", "時間軸使用秒；事件率按每分鐘正規化，可比較512 Hz與1024 Hz資料。所有震動圖共用由全資料最大絕對值推導的對稱y軸。"),
                ("改善率定義", "對於越低越好的指標：改善率 = (benchmark - August) / benchmark x 100%。正值表示改善，負值表示退步。"),
            ],
        )
        for title, labels in COMPARISONS.items():
            timeseries_page(pdf, title, labels, series, y_limit)

        august_labels = ["2026-08-27 上手 J5", "2026-08-27 下手 J4"]
        august = metrics.loc[august_labels, ["RMS_g", "P99_abs_g", "P99.9_abs_g", "Max_abs_g", "Within_pct", "Exceed_pct", "Exceed_seconds", "Events_per_min", "Max_event_ms"]]
        dataframe_page(
            pdf,
            "2026-08-27（250片）量化結果",
            "Within_pct 為嚴格 |g| < 0.3 g 的樣本比例；此頁不是265片驗收結果",
            august,
            {column: "{:.3f}" for column in august.columns},
        )
        improvement_page(pdf, "3月基準 → 8月：上手改善率", march_upper)
        improvement_page(pdf, "3月基準 → 8月：下手改善率", march_lower)
        improvement_page(pdf, "6月平均基準 → 8月：上手改善率", june_upper)
        improvement_page(pdf, "6月平均基準 → 8月：下手改善率", june_lower)

        conclusion = pd.DataFrame(
            {
                "Comparison": ["3月→8月 上手", "3月→8月 下手", "6月→8月 上手", "6月→8月 下手"],
                "RMS X/Y/Z improvement (%)": [
                    "/".join(f"{v:.1f}" for v in march_upper.RMS_g_improvement),
                    "/".join(f"{v:.1f}" for v in march_lower.RMS_g_improvement),
                    "/".join(f"{v:.1f}" for v in june_upper.RMS_g_improvement),
                    "/".join(f"{v:.1f}" for v in june_lower.RMS_g_improvement),
                ],
                "Exceed-rate X/Y/Z improvement (%)": [
                    "/".join(f"{v:.1f}" for v in march_upper.Exceed_pct_improvement),
                    "/".join(f"{v:.1f}" for v in march_lower.Exceed_pct_improvement),
                    "/".join(f"{v:.1f}" for v in june_upper.Exceed_pct_improvement),
                    "/".join(f"{v:.1f}" for v in june_lower.Exceed_pct_improvement),
                ],
            }
        ).set_index("Comparison")
        dataframe_page(
            pdf,
            "改善率總表與工程判讀",
            "X/Y/Z依序列示；正值為改善，負值為退步。下手Y為優先改善與複驗項目。",
            conclusion,
            {},
        )
        text_page(
            pdf,
            "結論與建議",
            [
                ("整體判斷", "7月底機構調整未呈現所有手臂與軸向的一致改善。相對6月，上手X/Z部分尾端或超標指標改善，但上手Y及下手X/Y有退步；下手Y的惡化最顯著。"),
                ("規格判定", "8月250片資料的六個軸向均出現 >=0.3 g 樣本，因此若規格定義為任何瞬時樣本皆須低於0.3 g，現況不符合；且尚未完成265片負載驗證。"),
                ("下一步", "1. 與廠商凍結0.3 g的計算定義與濾波頻帶。\n2. 在相同軌跡、速度、sensor orientation與取樣率下重測250片。\n3. 優先定位下手Y的振源與超標事件發生動作段。\n4. 改善確認後，以265片進行至少三次重複驗收。"),
            ],
        )

    print(OUTPUT)


if __name__ == "__main__":
    main()
