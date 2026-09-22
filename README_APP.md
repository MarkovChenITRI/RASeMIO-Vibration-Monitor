# WR503 Status & Vibration

Portable Windows application with exactly two tabs. Robot access is restricted to the read-only RASeMIO `GPST` command.

## Run and package

```powershell
uv run python run_app.py
uv run pyinstaller --noconfirm WR503-GPST-Monitor.spec
```

The packaged application is `dist\WR503-GPST-Monitor.exe`. Its persistent history is `wr503_status.sqlite3` beside the EXE. Development mode stores the database at the repository root.

## Observation History

1. Configure only the target host IP and RASeMIO port. The app reads active Windows adapters and their subnet masks, then automatically binds the most-specific adapter whose subnet contains the target.
2. Use **連線** to start the observation connection. Every successful GPST response is automatically saved; there is no separate capture button.
3. The large tile is gray with `--` while disconnected and green with mode `0/1/2/3` while connected.
4. Temporary failures turn the tile gray, leave a real history gap, and retry automatically.
5. Preview the read-only history newest-first, with time/mode filters and pagination.

The TCP timeout (3 seconds), retry interval (10 seconds), five-failure stop policy, and GPST polling interval (0.1 seconds / 10 Hz) are internal monitoring defaults and are not operator settings.

The observation table exposes only UTC timestamp and numeric mode. It has an internal row id for stable persistence.

## Vibration Analysis

1. Import a Sensor CSV.
2. Confirm the parsed local start time. Enter it manually if the CSV has no Date/Time metadata.
3. Set low-pass cutoff and target sampling rate (defaults: 200 Hz and 512 Hz). The cutoff must be below both source and target Nyquist frequencies.
4. With overlapping status history, optionally select upper-only mode 1, lower-only mode 2, and/or both-arms mode 3. Multiple selections form a union; no selection analyzes the entire CSV.
5. Unknown status intervals have gray plot backgrounds. Covered intervals are shaded by GPST: light blue for `0` (no wafer), dark blue for `≥1` (wafer held). Colored curves and metrics use the same independently processed contiguous intervals.
6. The sensor start time is only known to the second and the sensor and RASeMIO clocks are separate, so the GPST mask can be offset from the vibration data. Left-drag on the plot to slide the mask until wafer pick/place lines up with the waveform; releasing re-runs the analysis with that offset. The offset (seconds, positive = GPST later) is also editable in **GPST 對時偏移 (s)**, resets with **偏移歸零**, and resets to 0 when a new CSV is imported. Scroll-zoom is kept across re-analysis so alignment can be done zoomed in. The whole-recording range is always analyzed; the former drag-to-crop range was replaced by this alignment drag.

If the entire Sensor interval has no status history, mode controls are disabled and full-recording analysis remains available. Results include X/Y/Z RMS, P99, P99.9, max absolute acceleration, threshold numerator/denominator, percentages, event count, analyzed duration, events/minute, and longest event duration. Export options are PNG, metrics CSV, and aligned samples CSV; no notebook or PDF is generated.
