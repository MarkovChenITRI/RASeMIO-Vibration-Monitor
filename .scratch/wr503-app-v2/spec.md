Status: ready-for-agent

# WR503 observation and vibration analysis app v2

## Problem Statement

The current desktop tool records GPST data to per-session CSV files and offers a manual replay view. It does not provide a durable, continuously queryable status history, and its vibration workflow still requires manual alignment and does not reproduce the standardized notebook metrics within the application. Operators need one small portable Windows application that records pickup mode history, previews it, imports a sensor recording, aligns both sources despite non-identical timestamps, filters by wafer state, and presents auditable vibration metrics without generating notebooks or PDF reports.

## Solution

Replace the current workflow with two tabs. The Observation History tab configures and verifies the read-only RASeMIO connection, starts and stops continuous GPST acquisition, writes every successful status observation observation into one SQLite database beside the executable, and provides a read-only paginated preview. The Vibration Analysis tab imports one sensor CSV, resolves its absolute local-time interval, finds overlapping status history, visually distinguishes covered and uncovered periods, applies optional pickup-mode filters using interval membership rather than timestamp equality, runs configurable standardized signal processing, and presents X/Y/Z plots plus auditable technical indicators and exports.

## User Stories

1. As an operator, I want exactly two tabs, so that observation and analysis remain easy to distinguish.
2. As an operator, I want to configure the RASeMIO host, port, optional local IP, timeout, and polling interval, so that the tool works with the machine network.
3. As an operator, I want to test the connection before acquisition, so that I can see whether GPST is reachable.
4. As an operator, I want a large live pickup-mode display, so that I can observe the machine from a distance.
5. As an operator, I want all robot communication to remain read-only GPST, so that this tool cannot move the robot or write I/O.
6. As an operator, I want acquisition to start and stop explicitly, so that I control the recorded interval.
7. As an analyst, I want every successful poll stored even if the mode is unchanged, so that freshness and connection gaps can be inferred.
8. As an analyst, I want only observation time and pickup mode treated as recorded domain data, so that the history stays minimal.
9. As an operator, I want the database beside the executable, so that the portable app and its data stay together.
10. As an operator, I want acquisition to reconnect after transient failure, so that a brief disconnect does not end a run.
11. As an analyst, I want disconnect gaps left empty, so that stale modes are not extended through unknown periods.
12. As an operator, I want to preview status history newest-first, so that recent acquisition is immediately visible.
13. As an operator, I want pagination and time/mode query controls, so that a large history remains usable.
14. As an operator, I do not want editing controls in this version, so that the initial database UI remains simple.
15. As an analyst, I want to import the sensor application's CSV, so that vibration can be analyzed without a notebook.
16. As an analyst, I want the parsed CSV start, end, duration, and inferred sample rate displayed, so that time interpretation errors are visible.
17. As an analyst, I want CSV header Date/Time interpreted as local machine time, so that it can align with status history.
18. As an analyst, I want to enter the sensor start time when Date/Time is missing, so that undated recordings can still be aligned.
19. As an analyst, I want status intervals inferred from each observation until the next observation, so that exact timestamp equality is unnecessary.
20. As an analyst, I want a status observation considered stale after three expected polling periods, so that gaps remain unknown.
21. As an analyst, I want mode filters disabled when the entire sensor interval has no status history, so that unavailable filtering cannot be selected.
22. As an analyst, I want full-CSV analysis to remain available when no status history overlaps, so that vibration analysis is not blocked.
23. As an analyst, I want partially overlapping history to enable mode filtering, so that the covered portion remains useful.
24. As an analyst, I want uncovered time shown with a gray plot background and covered time shown white, so that data provenance is visible.
25. As an analyst, I want covered but unselected modes shown with subdued traces rather than gray background, so that coverage is not confused with selection.
26. As an analyst, I want separate filters for mode 1 upper-only, mode 2 lower-only, and mode 3 both arms, so that wafer configurations can be isolated.
27. As an analyst, I want multiple selected modes combined as a union, so that several wafer configurations can be analyzed together.
28. As an analyst, I want no selected modes to mean the complete sensor recording, so that the default remains understandable.
29. As an analyst, I want coverage duration, selected duration, and total CSV duration displayed as numerator/denominator values, so that filtered evidence is auditable.
30. As an analyst, I want low-pass cutoff and resampling rate editable in the UI with defaults of 200 Hz and 512 Hz, so that processing can be adjusted.
31. As an analyst, I want invalid Nyquist combinations rejected with an explanation, so that the app never silently alters settings.
32. As an analyst, I want each selected contiguous interval processed independently, so that filtering does not create artifacts across gaps.
33. As an analyst, I want RMS, P99 absolute, P99.9 absolute, and maximum absolute acceleration displayed for X/Y/Z, so that vibration magnitude is quantified.
34. As an analyst, I want within-threshold and exceedance sample counts shown as numerator/denominator plus percentages, so that ratios are transparent.
35. As an analyst, I want exceedance event count, analyzed seconds, events per minute, and longest event duration, so that threshold behavior is quantified.
36. As an analyst, I want exceedance events reset at interval boundaries, so that disconnected periods cannot join separate events.
37. As an analyst, I want ±0.3 g threshold lines on all axis plots, so that the vendor limit is visible.
38. As an analyst, I want processing and selection summaries displayed beside the results, so that I know what produced each metric.
39. As an analyst, I want to export the current plot as PNG, metrics as CSV, and aligned sample data as CSV, so that results can be shared without notebooks or PDFs.
40. As an operator, I want ordinary, visible errors if the database beside the executable cannot be written, so that failures are understandable without extra storage policy.

## Implementation Decisions

- The application has exactly two top-level tabs: Observation History and Vibration Analysis.
- RASeMIO access is restricted to the read-only GPST request and reads response mode.
- A single portable SQLite database resides beside the executable. In development mode it resides beside the launcher.
- The observation table has an internal integer primary key plus an indexed absolute timestamp and integer mode. Only timestamp and mode are user-facing observation data.
- Every successful GPST response during acquisition is inserted and committed in batches without blocking the UI thread.
- Temporary connection failure triggers bounded-delay reconnect attempts; no observations are synthesized during gaps.
- History is read-only in this version and supports newest-first pagination plus time and mode filters.
- Sensor CSV metadata and time columns are parsed through one sensor-import interface. Naive dates are interpreted in the machine's local timezone.
- When metadata lacks an absolute start, the UI requests a local start datetime before querying history.
- Alignment assigns to each raw sensor sample the most recent prior status observation. A status remains valid no longer than three configured polling intervals.
- The vibration view distinguishes status coverage from mode selection. Unknown coverage is gray; known coverage is white; known but unselected samples are visually subdued.
- Filter selections map exactly: upper-only to mode 1, lower-only to mode 2, both arms to mode 3. Multiple selections form a union. No selection analyzes the entire CSV.
- If the sensor interval has no overlapping status observations, all mode controls are disabled and full-recording analysis remains active.
- Partial overlap enables filters and excludes unknown samples only when a mode filter is active.
- Filtering and resampling run independently on each contiguous selected interval. Aggregate metrics combine interval results, while event detection resets per interval.
- Low-pass cutoff and target sampling rate are user parameters defaulting to 200 Hz and 512 Hz. Both must be positive, and cutoff must be below both the source and target Nyquist limits.
- Results show X/Y/Z plots, processing settings, time coverage fractions, and all agreed technical indicators with explicit counts and denominators.
- Exports are limited to plot PNG, metrics CSV, and aligned sample CSV. Notebook, PDF, and automatic report generation are excluded.
- The database preview and acquisition refresh through service interfaces; the UI does not issue raw SQL or perform socket I/O directly.

## Testing Decisions

- The primary seam is an application service exercised with a temporary SQLite database, a fake GPST source, and representative sensor frames. Tests assert externally observable history, alignment, selection, metrics, and export behavior rather than widget implementation.
- Protocol packet and CRC tests already present remain as lower-level protection for the hardware boundary.
- Database tests cover initialization, batch insertion, ordering, pagination, time filtering, mode filtering, and persistence across service instances.
- Acquisition tests use a fake source to cover repeated unchanged modes, disconnect gaps, reconnect, start/stop, and non-blocking delivery.
- Alignment tests cover exact and non-exact timestamps, leading/trailing gaps, staleness, zero overlap, partial overlap, local-time conversion, and manual start time.
- Selection tests cover no selection, each individual mode, every multi-mode union, covered-but-unselected samples, and unknown samples.
- Signal tests cover parameter validation, independent contiguous-segment processing, metric numerators/denominators, event boundary resets, and empty selections.
- Export tests verify schemas and values for metrics and aligned samples, plus creation of a non-empty PNG.
- A packaged-EXE smoke test verifies that the application starts and creates or opens the database beside itself.

## Out of Scope

- Editing, inserting, or deleting historical observations through the UI.
- Commands that move the robot, change robot state, or write I/O.
- Direct acquisition from the closed vibration sensor application.
- Hardware-level clock synchronization.
- Automatic notebook, PDF, or report generation.
- Formal 265-wafer acceptance certification.
- Automated interpretation labels for GPST modes beyond the agreed numeric mapping.
- Integrated video recording or synchronized video playback.
