Status: resolved
Blocked by: none

# 01 — Persist status observations

Deliver a demoable acquisition path that tests a read-only GPST connection, starts and stops capture, stores every successful observation in the portable SQLite database, and reconnects without inventing samples during gaps.

- [ ] Database is created beside the executable with timestamp and mode observations.
- [ ] Acquisition remains off until explicitly started and survives transient disconnects.
- [ ] Public service tests cover repeated modes, restart persistence, and gaps.
