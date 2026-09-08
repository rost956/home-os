# Checkpoint A2 — user settings and financial period

Checkpoint A2 is complete. Per-user settings are stored on the existing `users`
record: `theme` (`system`, `light`, or `dark`) and
`expense_period_start_day` (1–31). The authenticated `/settings` page manages
both values.

Financial summaries, analytics defaults, limits, and forecasts use the shared
financial-period helper. A missing selected day (29–31) is clamped to the last
day of that month, keeping periods contiguous.

No new migration was required: both user columns already existed. New database
defaults use `system` for theme and `1` for the period start day.

The next checkpoint is B: finance analytics category drill-down.
