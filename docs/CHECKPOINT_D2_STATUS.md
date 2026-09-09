# Checkpoint D2 — recurring Planner events

Recurring Planner events remain one canonical `planner_items` row. Optional `recurrence_frequency` (`daily`, `weekly`, `monthly`, `yearly`), `recurrence_interval`, and inclusive `recurrence_until` derive occurrences at render time; no generated occurrences are stored.

Generation is bounded by the requested range and anchored to the canonical start. Monthly dates clamp only in the target month, so a 31 January series produces 28 February then 31 March. Yearly 29 February clamps to 28 February in non-leap years and returns to 29 February in leap years. Multi-day occurrences preserve the D1 inclusive duration.

The Planner month grid, selected day, and upcoming list use derived occurrences. Create/edit changes the whole series; deletion removes the canonical series. SQLite migration adds nullable frequency/until and interval defaulting to one; backup export/import retains the fields.

Limitations: no per-occurrence exceptions, no multi-weekday rules, and no reminders/push (D3).
