# Checkpoint D3.1 — Planner reminder foundation

Planner events now own zero or more canonical reminder configurations. Each `planner_reminders` row stores an integer offset, its `minutes`/`hours`/`days` unit, and the fixed `before_start` relation. Configurations belong to the canonical `PlannerItem`; recurring occurrences and delivery-state rows are not persisted. Removing a configuration leaves the event intact, while deleting the event cascades to its configurations.

The deterministic reminder service expands D2 occurrences only inside the requested range plus the maximum allowed 365-day offset. It returns `DuePlannerReminder` values for the half-open interval `[window_start, window_end)`, scoped by event owner, with a stable key composed from reminder ID and occurrence key. Monthly day clamping, leap-day clamping, recurrence intervals and inclusive `recurrence_until` therefore remain identical to the Planner calendar. Multi-day reminders are based on the occurrence start, not its end.

Planner dates and times remain local to the application's existing `Europe/Moscow` contract. Timed events use `start_time`; all-day events start at local midnight. Due-query bounds and returned datetimes are timezone-aware, preventing implicit mixing of naive UTC and local wall time. Per-user timezones are intentionally outside this checkpoint.

Create and edit forms support multiple reminder rows, custom non-negative integer offsets, zero for “at start”, supported units, and removal. Server validation rejects unsupported units, values beyond 365 days, and semantically duplicate offsets such as 60 minutes plus 1 hour. Event cards and upcoming entries show compact reminder information. Editing recurring event reminders updates the whole canonical series.

Runtime SQLite migration creates the new table without rewriting `planner_items`; the existing pre-migration backup hook detects the missing metadata table. JSON export/import includes nested reminder configurations and remains compatible with older exports that omit them.

This checkpoint does not deliver notifications. Web Push subscription, service-worker delivery, background scheduling, deduplication of delivered notifications, and permission UX belong to D3.2.
