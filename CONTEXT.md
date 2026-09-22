# WR503 Wafer Vibration Observation

This context covers observation of wafer pickup state and analysis of vibration recordings for the WR503 dual-arm wafer robot.

## Language

**Status observation**:
A pickup mode observed from one successful GPST response at a particular instant.
_Avoid_: IO sample, pickup record

**Status history**:
The ordered sequence of status observations used to infer the intervals during which each pickup mode was active.
_Avoid_: GPST CSV, recording log

**Observation connection**:
The live read-only GPST connection. While it is connected, every successful response is automatically part of status history; there is no separate recording process.
_Avoid_: Recording session, capture job

**Sensor recording**:
A vibration time series exported by the closed sensor application, containing time and X/Y/Z acceleration.
_Avoid_: sensor log, raw database

**Mode interval**:
The half-open time interval from one status observation until the next observation, during which the earlier pickup mode is considered active.
_Avoid_: exact timestamp match

**Analysis selection**:
The union of Sensor recording regions whose overlapping Mode intervals match the selected pickup modes.
_Avoid_: arm filter

**Status coverage**:
The portion of a Sensor recording for which a sufficiently recent status observation exists. A disconnected or stale interval is outside coverage.
_Avoid_: complete overlap
