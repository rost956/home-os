# Checkpoint D1 — multi-day Planner events

Planner events keep one canonical database record. `planner_items.end_date` is nullable: an absent value means the existing single-day event at `scheduled_for`.

Dates form an inclusive interval. Shared helpers provide effective end dates, day membership, overlap checks, duration, and bounded expansion into visible month cells. Calendar retrieval uses interval overlap, so events crossing months and years are visible on both sides without per-day database queries.

The Planner create and edit forms accept start and end dates; equal dates remain single-day events. The server rejects an end before the start while retaining submitted form data. Month cells render every in-range occurrence, but day panels and upcoming lists retain one canonical event; upcoming events remain active through their effective end date. Existing owner checks continue to scope all planner operations.

For existing SQLite databases, the runtime schema updater adds nullable `end_date` non-destructively. Backup export/import retains the field. `/today` does not currently contain Planner integration, so D1 does not expand that page.

Limitations: recurrence is not implemented (D2); reminders and push notifications are not implemented (D3).
